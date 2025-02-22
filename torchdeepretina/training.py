import os
import numpy as np
import torch
import torch.nn as nn
from torch.nn import PoissonNLLLoss, MSELoss
import torch.nn.functional as F
import torchdeepretina.utils as utils
import torchdeepretina.io as tdrio
import torchdeepretina.pruning as tdrprune
import torchdeepretina.intracellular as tdrintr
from torchdeepretina.datas import loadintrexpt,loadexpt,DataContainer,\
                                                       DataDistributor
from torchdeepretina.custom_modules import semantic_loss,NullScheduler
from torchdeepretina.models import *
import torchdeepretina.analysis as analysis
from torch.optim.lr_scheduler import ReduceLROnPlateau,StepLR,MultiStepLR
import time
from tqdm import tqdm
import math
import torch.multiprocessing as mp
from queue import Queue
from collections import deque
import psutil
import gc
import resource
import json

if torch.cuda.is_available():
    DEVICE = torch.device("cuda:0")
else:
    DEVICE = torch.device("cpu")


class Trainer:
    def __init__(self, run_q=None, return_q=None, early_stopping=10,
                                               stop_tolerance=0.01):
        """
        run_q: Queue of dicts
            holds the hyperparameter dicts for each training
        return_q: empty Queue
            the results of the training are placed in this queue
        early_stopping: int
            the number of epochs to wait before doing early stopping
        stop_tolerance: float
            the amount that the loss must increase by to reset the
            early stopping epoch count
        """
        self.run_q = run_q
        self.ret_q = return_q
        self.early_stopping = early_stopping
        self.tolerance = stop_tolerance
        self.prev_acc = None

    def train(self,hyps,verbose=True):
        if utils.try_key(hyps, "retinotopic", False):
            train_method = getattr(self, "retinotopic_loop")
        else:
            train_method = self.train_loop
        try:
            return train_method(hyps,verbose)
        except IndexError as e:
            print("Caught Index Error", e)
            return None

    def stop_early(self, acc):
        """
        The early stopping function

        acc: float
            the accuracy or loss for the most recent epoch
        """
        if self.early_stopping <= 0:
            return False # use 0 as way to cancel early stopping
        if self.prev_acc is None:
            self.prev_acc = acc
            self.stop_count = 0
            return False
        if acc-self.prev_acc < self.tolerance:
            self.stop_count += 1
            if self.stop_count >= self.early_stopping:
                return True
        self.stop_count = 0
        self.prev_acc = acc
        return False

    def train_loop(self, hyps, verbose=False):
        """
        hyps: dict
            dict of relevant hyperparameters
        """
        # Initialize miscellaneous parameters
        torch.cuda.empty_cache()
        batch_size = hyps['batch_size']

        hyps['seed'] = utils.try_key(hyps,'seed',None)
        if hyps['seed'] is None:
            hyps['seed'] = int(time.time())

        if hyps['cross_val']:
            cross_val_range = range(hyps['n_cv_folds'])
        else:
            cross_val_idx = hyps['cross_val_idx']
            if cross_val_idx is None: cross_val_idx = 0
            cross_val_idx = min(hyps['n_cv_folds']-1,cross_val_idx)
            cross_val_range = range(cross_val_idx,cross_val_idx+1)
        for cv_idx in cross_val_range:
            hyps['cross_val_idx'] = cv_idx
            hyps['exp_num'] = tdrio.get_next_exp_num(hyps['exp_name'])
            if 'exp_num_offset' in hyps:
                hyps['exp_num'] += int(hyps['exp_num_offset'])
                while tdrio.exp_num_exists(hyps['exp_num'],
                                           hyps['exp_name']):
                    hyps['exp_num']+=1
            # Set manual seed
            if hyps['seed'] == "exp_num":
                hyps['seed'] = hyps['exp_num']
            torch.manual_seed(hyps['seed'])
            np.random.seed(hyps['seed'])

            hyps['save_folder'] = tdrio.make_save_folder(hyps)
            s = "Beginning training for {} -- CV {}"
            s = s.format(hyps['save_folder'],cv_idx)
            print(s)

            # Get Data, Make Model, Record Initial Hyps and Model
            train_data, test_data = get_data(hyps)
            hyps["n_units"] = train_data.y.shape[-1]
            hyps['centers'] = train_data.centers
            model, data_distr = get_model_and_distr(hyps, train_data)
            # Make optimization objects (lossfxn, optimizer, scheduler)
            optimizer, scheduler, loss_fn = get_optim_objs(hyps, model,
                                                    train_data.centers)
            if 'startpt' in hyps and hyps['startpt'] is not None:
                checkpt = tdrio.load_checkpoint(hyps['startpt'])
                model.load_state_dict(checkpt['model_state_dict'])
                optimizer.load_state_dict(checkpt['optim_state_dict'])

            og_state_dict = model.state_dict()
            og_optim_dict = optimizer.state_dict()

            print("train shape:", data_distr.train_shape)
            print("val shape:", data_distr.val_shape)
            record_session(hyps, model)

            # Initializations
            if hyps['exp_name'] == "test":
                hyps['n_epochs'] = 2
                hyps['prune_intvl'] = 2
            if hyps['prune_layers'] == 'all' or hyps['prune_layers']==[]:
                layers = utils.get_conv_layer_names(model)
                hyps['prune_layers'] = layers[:-1]
                hyps['open_layers'] = set(hyps['prune_layers'])

            hyps_prune = utils.try_key(hyps, 'prune', False)
            reset_sd = utils.try_key(hyps,'reset_sd',False)
            min_prune_acc = utils.try_key(hyps,'min_prune_acc',0)
            if min_prune_acc is None: min_prune_acc = 0
            reset_lr = utils.try_key(hyps,'reset_lr',False)
            reset_lr = reset_lr and not isinstance(scheduler,
                                               NullScheduler)
            n_epochs = hyps['n_epochs']
            if hyps_prune and reset_sd:
                n_epochs = hyps['prune_intvl']

            zero_dict = {d:set() for d in hyps['prune_layers']}
            zero_bias = utils.try_key(hyps,'zero_bias',True)
            if 'startpt' in hyps and hyps['startpt'] is not None:
                checkpt = tdrio.load_checkpoint(hyps['startpt'])
                if 'zero_dict' in checkpt:
                    zero_dict = checkpt['zero_dict']
                if 'zero_bias' in checkpt['hyps']:
                    hyps['zero_bias'] = checkpt['hyps']['zero_bias']

            # Training Loop
            epoch = -1
            stop_training = False
            while not stop_training:
                epoch += 1
                stop_training = epoch >= n_epochs and not hyps_prune
                cv_s = "-- CV {}/{}".format(cv_idx, hyps['n_cv_folds'])
                print("Beginning Epoch {}/{} -- ".format(epoch,n_epochs),
                                                     hyps['save_folder'],
                                                     cv_s)
                print()
                n_loops = data_distr.n_loops
                model.train(mode=True)
                epoch_loss = 0
                sf = hyps['save_folder']
                stats_string = 'Epoch {} -- {} {}\n'.format(epoch, sf,
                                                                 cv_s)
                starttime = time.time()

                # Train Loop
                for i,(x,label) in enumerate(data_distr.train_sample()):
                    optimizer.zero_grad()
                    label = label.float()

                    # Error Evaluation
                    y,error = static_eval(x, label, model, loss_fn)
                    if hyps['l1']<=0:
                        activity_l1 = torch.zeros(1).to(DEVICE)
                    else:
                        activity_l1 = hyps['l1']*torch.norm(y, 1).float()
                        activity_l1 = activity_l1.mean()
                    if 'gauss_reg' in hyps and hyps['gauss_reg'] > 0:
                        g_coef = hyps['gauss_loss_coef']
                        activity_l1 += g_coef*gauss_reg.get_loss()

                    # Backwards Pass
                    loss = error + activity_l1
                    loss.backward()
                    optimizer.step()
                    # Only prunes if zero_dict contains values
                    tdrprune.zero_chans(model, zero_dict, zero_bias)

                    epoch_loss += loss.item()
                    if verbose:
                        print_train_update(error, activity_l1, model,
                                                          n_loops, i)
                    if math.isnan(epoch_loss) or math.isinf(epoch_loss):
                        break
                    if hyps['exp_name']=="test" and i >= 5:
                        break

                # Clean Up Train Loop
                avg_loss = epoch_loss/n_loops
                s = 'Avg Loss: {} -- Time: {}\n'
                stats_string += s.format(avg_loss, time.time()-starttime)
                # Deletions for memory reduction
                del x
                del y
                del label

                # Validation
                model.eval()
                with torch.no_grad():
                    # Miscellaneous Initializations
                    selfattn = utils.try_key(hyps,'self_attn',False)
                    step_size = 128 if selfattn else 500
                    n_loops = data_distr.val_shape[0]//step_size
                    if verbose:
                        print()
                        print("Validating")

                    # Validation Block
                    tup = validate_static(hyps,model,data_distr,
                                         loss_fn, step_size=step_size,
                                         verbose=verbose)
                    val_loss, val_preds, val_targs = tup
                    # Validation Evaluation
                    val_loss = val_loss/n_loops
                    n_units = data_distr.val_y.shape[-1]
                    val_preds = np.concatenate(val_preds, axis=0)
                    val_targs = np.concatenate(val_targs, axis=0)
                    pearsons = utils.pearsonr(val_preds, val_targs)
                    s = " | ".join([str(p) for p in pearsons])
                    stats_string += "Val Cell Cors:" + s +'\n'
                    val_acc = np.mean(pearsons)
                    stop = self.stop_early(val_acc)

                    # Clean Up
                    s = "Val Cor: {} | Val Loss: {}\n"
                    stats_string += s.format(val_acc, val_loss)
                    scheduler.step(val_acc)
                    del val_preds

                    # Validation on Test Subset (Nonrecurrent Models Only)
                    avg_pearson = 0
                    if test_data is not None:
                        test_x = torch.from_numpy(test_data.X)
                        test_obs = utils.inspect(model,
                                               X=test_x,
                                               batch_size=step_size,
                                               to_numpy=True)['outputs']
                        rng = range(test_obs.shape[-1])
                        pearsons = utils.pearsonr(test_obs,test_data.y)
                        for cell,r in enumerate(pearsons):
                            avg_pearson += r
                            s = 'Cell ' + str(cell) + ': ' + str(r)+"\n"
                            stats_string += s
                        n = float(test_obs.shape[-1])
                        avg_pearson = avg_pearson / n
                        s = "Avg Test Pearson: "+ str(avg_pearson) + "\n"
                        stats_string += s
                        del test_obs

                # Integrated Gradient Pruning
                prune = hyps_prune and epoch >= n_epochs-1
                intvl = (epoch-n_epochs) % hyps['prune_intvl'] == 0
                if prune and intvl:
                    if epoch <= (n_epochs+hyps['prune_intvl']):
                        prune_dict = { "zero_dict":zero_dict,
                                       "prev_state_dict":None,
                                       "cur_chan":-1,
                                       "cur_layer":None,
                                       "prev_acc":-1,
                                       "prev_lr":cur_lr,
                                       "min_acc":min_prune_acc}
                        if hyps['exp_name'] == "test":
                            val_acc = 0

                    cur_lr = next(iter(optimizer.param_groups))['lr']


                    bsize = hyps['intg_bsize']
                    gen = data_distr.train_sample(batch_size=bsize)
                    (data_sample, _) = next(gen)
                    del gen
                    prune_dict = tdrprune.prune_channels(model, hyps,
                                                       data_sample,
                                                       val_acc=val_acc,
                                                       lr=cur_lr,
                                                       **prune_dict)
                    stop_training = len(hyps['open_layers'])==0
                    if reset_sd:
                        model.load_state_dict(og_state_dict)
                        optimizer.load_state_dict(og_optim_dict)
                        for param_group in optimizer.param_groups:
                            param_group['lr'] = hyps['lr']
                    else:
                        for param_group in optimizer.param_groups:
                            param_group['lr'] = prune_dict['prev_lr']

                    if reset_lr and prune_dict['prev_acc']!=val_acc:
                        # Reset learning rate if using scheduler
                        # and latest pruning was neglected
                        for param_group in optimizer.param_groups:
                            param_group['lr'] = hyps['lr']
                    zero_dict = prune_dict['zero_dict']
                    zero_bias = utils.try_key(hyps,'zero_bias',True)
                    tdrprune.zero_chans(model, zero_dict,zero_bias)

                    # In this case we want to train out the final
                    # pruned model using the normal number of epochs.
                    # This will make the training look like a
                    # non-pruning training while using the pruned model.
                    if stop_training and reset_sd:
                        stop_training = False
                        hyps_prune = False
                        n_epochs = hyps['n_epochs'] + epoch

                # Print Epoch Stats
                if prune or (not hyps_prune and hyps['prune']):
                    s = "Zeroed Channels:\n"
                    keys = sorted(list(zero_dict.keys()))
                    for k in keys:
                        chans = [str(c) for c in zero_dict[k]]
                        s += "{}: {}\n".format(k,",".join(chans))
                    stats_string += s
                gc.collect()
                max_mem_used = resource.getrusage(resource.RUSAGE_SELF)
                max_mem_used = max_mem_used.ru_maxrss/1024
                s = "Memory Used: {:.2f} mb\n"
                stats_string += s.format(max_mem_used)
                print(stats_string)

                # Log progress to txt file
                log = os.path.join(hyps['save_folder'],"training_log.txt")
                with open(log,'a') as f:
                    f.write(str(stats_string)+'\n')

                # Final Model Snapshot
                cur_lr = next(iter(optimizer.param_groups))['lr']
                optimizer.zero_grad()
                save_dict = {
                    "model_type": hyps['model_type'],
                    "model_state_dict":model.state_dict(),
                    "optim_state_dict":optimizer.state_dict(),
                    "hyps": hyps,
                    "loss": avg_loss,
                    "epoch":epoch,
                    "val_loss":val_loss,
                    "val_acc":val_acc,
                    "test_pearson":avg_pearson,
                    "norm_stats":train_data.stats,
                    "zero_dict":zero_dict,
                    "y_stats":{'mean':data_distr.y_mean,
                                 'std':data_distr.y_std},
                    "cur_lr":cur_lr, # Current LR
                }
                for k in hyps.keys():
                    if k not in save_dict:
                        save_dict[k] = hyps[k]
                del_prev = 'save_every_epoch' in hyps and\
                                            not hyps['save_every_epoch']
                tdrio.save_checkpoint(save_dict, hyps['save_folder'],
                                           'test', del_prev=del_prev)

                # If loss is nan, training is futile
                if math.isnan(avg_loss) or math.isinf(avg_loss) or stop:
                    break


            # Final save
            results = {"save_folder":hyps['save_folder'],
                        "Loss":avg_loss,
                        "ValAcc":val_acc,
                        "ValLoss":val_loss,
                        "TestPearson":avg_pearson,
                        **zero_dict}
            with open(hyps['save_folder'] + "/hyperparams.txt",'a') as f:
                s = " ".join([str(k)+":"+str(results[k]) for k in\
                                          sorted(results.keys())])
                if hyps['prune']:
                    s += "\nZeroed Channels:\n"
                    keys = sorted(list(zero_dict.keys()))
                    for k in keys:
                        chans = [str(c) for c in zero_dict[k]]
                        s += "{}: {}\n".format(k,",".join(chans))
                s = "\n" + s + '\n'
                f.write(s)
        return results

    def retinotopic_loop(self, hyps, verbose=False):
        """
        hyps: dict
            dict of relevant hyperparameters
        """
        # Initialize miscellaneous parameters
        torch.cuda.empty_cache()
        batch_size = hyps['batch_size']

        hyps['exp_num'] = tdrio.get_next_exp_num(hyps['exp_name'])
        if 'exp_num_offset' in hyps:
                hyps['exp_num'] += hyps['exp_num_offset']
        hyps['save_folder'] = tdrio.make_save_folder(hyps)
        print("Beginning training for {}".format(hyps['save_folder']))

        # Get Data, Make Model, Record Initial Hyps and Model
        train_data, test_data = get_data(hyps)
        hyps["n_units"] = train_data.y.shape[-1]
        hyps['centers'] = train_data.centers
        model, data_distr = get_model_and_distr(hyps, train_data)
        print("train shape:", data_distr.train_shape)
        print("val shape:", data_distr.val_shape)

        record_session(hyps, model)

        # Make optimization objects (lossfxn, optimizer,
        optimizer, scheduler, loss_fn = get_optim_objs(hyps, model,
                                                train_data.centers)

        # Training
        if hyps['exp_name'] == "test":
            hyps['n_epochs'] = 4

        n_epochs = hyps['n_epochs']

        epoch = -1
        stop_training = False
        while not stop_training:
            epoch += 1
            stop_training = epoch >= n_epochs
            print("Beginning Epoch {}/{} -- ".format(epoch,n_epochs),
                                                 hyps['save_folder'])
            print()
            n_loops = data_distr.n_loops
            model.train(mode=True)
            epoch_loss = 0
            sf = hyps['save_folder']
            stats_string = 'Epoch {} -- {}\n'.format(epoch, sf)
            starttime = time.time()

            # Train Loop
            for i,(x,label) in enumerate(data_distr.train_sample()):
                optimizer.zero_grad()
                label = label.float()

                # Error Evaluation
                y,error = static_eval(x, label, model, loss_fn)
                if hyps['l1'] <= 0:
                    activity_l1 = torch.zeros(1).to(DEVICE)
                else:
                    activity_l1 = hyps['l1']*torch.norm(y, 1).float()
                    activity_l1 = activity_l1 .mean()

                # One Hot Loss
                scale = utils.try_key(hyps, 'semantic_scale', 10)
                if scale > 0:
                    one_hot_prob = model.sequential[-1].prob
                    one_hot_loss = semantic_loss(one_hot_prob)*scale
                else:
                    one_hot_loss = torch.zeros(1).to(DEVICE)

                # One Hot l1 penalty
                semantic_l1 = utils.try_key(hyps,'semantic_l1',0)
                if semantic_l1 > 0:
                    w = model.sequential[-1].w
                    l = torch.norm(w, 1, dim=-1).float()
                    one_hot_loss += semantic_l1*l.mean()

                loss = error + activity_l1 + one_hot_loss


                loss.backward()
                optimizer.step()

                epoch_loss += loss.item()

                if verbose:
                    print_train_update(error, activity_l1, model,
                                                      n_loops, i)


                if math.isnan(epoch_loss) or math.isinf(epoch_loss)\
                                        or hyps['exp_name']=="test":
                    break

            # Clean Up Train Loop
            avg_loss = epoch_loss/n_loops
            s = 'Avg Loss: {} -- Time: {}\n -- One Hot Loss: {}'

            LRs = []
            for LR_val in optimizer.param_groups:
                LRs.append(LR_val['lr'])

            stats_string += s.format(avg_loss, time.time()-starttime,
                                                    one_hot_loss,LRs)
            # Deletions for memory reduction
            del x
            del y
            del label

            # Validation
            model.eval()
            with torch.no_grad():
                # Miscellaneous Initializations
                step_size = 500
                n_loops = data_distr.val_shape[0]//step_size
                if verbose:
                    print()
                    print("Validating")

                # Validation Block
                tup = validate_static(hyps,model,data_distr,
                                     loss_fn, step_size=step_size,
                                     verbose=verbose)
                val_loss, val_preds, val_targs = tup
                # Validation Evaluation
                val_loss = val_loss/n_loops
                n_units = data_distr.val_y.shape[-1]
                val_preds = np.concatenate(val_preds, axis=0)
                val_targs = np.concatenate(val_targs, axis=0)
                pearsons = []

                pearsons = utils.pearsonr(val_preds, val_targs)
                s = " | ".join([str(p) for p in pearsons])
                stats_string += "Val Cell Cors:" + s +'\n'
                val_acc = np.mean(pearsons)
                stop = self.stop_early(val_acc)

                # Clean Up
                s = "Val Cor: {} | Val Loss: {}\n"
                stats_string += s.format(val_acc, val_loss)
                if hyps['scheduler'] == 'MultiStepLR':
                    print('Stepping MultiStepLR')
                    scheduler.step()
                else:
                    scheduler.step(val_acc)
                del val_preds

                # Validation on Test Subset (Nonrecurrent Models Only)
                avg_pearson = 0
                if test_data is not None:
                    test_x = torch.from_numpy(test_data.X)
                    test_obs = model(test_x.to(DEVICE)).cpu()
                    test_obs = test_obs.detach().numpy()
                    rng = range(test_obs.shape[-1])
                    pearsons = utils.pearsonr(test_obs,test_data.y)
                    for cell,r in enumerate(pearsons):
                        avg_pearson += r
                        s = 'Cell ' + str(cell) + ': ' + str(r)+"\n"
                        stats_string += s
                    n = float(test_obs.shape[-1])
                    avg_pearson = avg_pearson / n
                    s = "Avg Test Pearson: "+ str(avg_pearson) + "\n"
                    stats_string += s
                    del test_obs

            # Save Model Snapshot
            optimizer.zero_grad()
            save_dict = {
                "model_type": hyps['model_type'],
                "model_state_dict":model.state_dict(),
                "optim_state_dict":optimizer.state_dict(),
                "hyps": hyps,
                "loss": avg_loss,
                "epoch":epoch,
                "val_loss":val_loss,
                "val_acc":val_acc,
                "test_pearson":avg_pearson,
                "norm_stats":train_data.stats,
                "y_stats":{'mean':data_distr.y_mean,
                             'std':data_distr.y_std}
            }
            for k in hyps.keys():
                if k not in save_dict:
                    save_dict[k] = hyps[k]
            del_prev = 'save_every_epoch' in hyps and\
                                        not hyps['save_every_epoch']
            tdrio.save_checkpoint(save_dict, hyps['save_folder'],
                                       'test', del_prev=del_prev)

            # Print Epoch Stats
            gc.collect()
            max_mem_used = resource.getrusage(resource.RUSAGE_SELF)
            max_mem_used = max_mem_used.ru_maxrss/1024
            s = "Memory Used: {:.2f} mb\n"
            stats_string += s.format(max_mem_used)
            print(stats_string)

            # Log progress to txt file
            log = os.path.join(hyps['save_folder'],"training_log.txt")
            with open(log,'a') as f:
                f.write(str(stats_string)+'\n')
            # If loss is nan, training is futile
            if math.isnan(avg_loss) or math.isinf(avg_loss) or stop:
                break

        # Final save
        results = {"save_folder":hyps['save_folder'],
                    "Loss":avg_loss,
                    "ValAcc":val_acc,
                    "ValLoss":val_loss,
                    "TestPearson":avg_pearson}
        with open(hyps['save_folder'] + "/hyperparams.txt",'a') as f:
            s = " ".join([str(k)+":"+str(results[k]) for k in\
                                      sorted(results.keys())])
            if hyps['prune']:
                s += "\nZeroed Channels:\n"
                keys = sorted(list(zero_dict.keys()))
                for k in keys:
                    chans = [str(c) for c in zero_dict[k]]
                    s += "{}: {}\n".format(k,",".join(chans))
            s = "\n" + s + '\n'
            f.write(s)
        return results

def hyper_search(hyps, hyp_ranges, early_stopping=10,
                                 stop_tolerance=.01):
    """
    The top level function to create hyperparameter combinations and
    perform trainings.

    hyps: dict
        the initial hyperparameter dict
        keys: str
        vals: values for the hyperparameters specified by the keys
    hyp_ranges: dict
        these are the ranges that will change the hyperparameters for
        each search. A unique training is performed for every
        possible combination of the listed values for each key
        keys: str
        vals: lists of values for the hyperparameters specified by the
              keys
    early_stopping: int
        the number of epochs to wait before doing early stopping
    stop_tolerance: float
        the amount that the loss must increase by to reset the
        early stopping epoch count
    """
    starttime = time.time()
    exp_dir = os.path.abspath(os.path.expanduser(
        os.path.join(hyps.get("save_root", "./"), hyps['exp_name'])
    ))
    # Make results file
    if not os.path.exists(exp_dir):
        os.mkdir(exp_dir)
    results_file = os.path.join(exp_dir, "results.txt")
    with open(results_file,'a') as f:
        f.write("Hyperparameters:\n")
        for k in hyps.keys():
            if k not in hyp_ranges:
                f.write(str(k) + ": " + str(hyps[k]) + '\n')
        f.write("\nHyperranges:\n")
        for k in hyp_ranges.keys():
            rs = ",".join([str(v) for v in hyp_ranges[k]])
            s = str(k) + ": [" + rs +']\n'
            f.write(s)
        f.write('\n')

    hyper_q = Queue()
    hyper_q = fill_hyper_q(hyps, hyp_ranges, list(hyp_ranges.keys()),
                                                      hyper_q, idx=0)
    total_searches = hyper_q.qsize()
    print("n_searches:", total_searches)

    es = early_stopping
    st = stop_tolerance
    trainer = Trainer(early_stopping=es, stop_tolerance=st)

    result_count = 0
    print("Starting Hyperloop")
    while not hyper_q.empty():
        print("Searches left:", hyper_q.qsize(),"-- Running Time:",
                                             time.time()-starttime)
        hyps = hyper_q.get()
        results = trainer.train(hyps, verbose=True)
        if results is not None:
            with open(results_file,'a') as f:
                results = " -- ".join([str(k)+":"+str(results[k]) for\
                                         k in sorted(results.keys())])
                f.write("\n"+results+"\n")

        else:
            print("Skipped {} cells {}".format(hyps['dataset'],
                                               hyps['cells']))

def print_train_update(error, l1, model, n_loops, i):
    loss = error +  l1
    s = "Loss: {:.5e}".format(loss.item())
    if model.kinetic:
        ps = model.kinetics.named_parameters()
        ps = [str(name)+":"+str(round(p.data.item(),4)) for name,p\
                                                       in list(ps)]
        s += " | "+" | ".join(ps)
    s = "{} | {}/{}".format(s,i,n_loops)
    print(s, end="       \r")

def record_session(hyps, model):
    """
    Writes important parameters to file.

    hyps: dict
        dict of relevant hyperparameters
    model: torch nn.Module
        the model to be trained
    """
    sf = hyps['save_folder']
    if not os.path.exists(sf):
        os.mkdir(sf)
    h = "hyperparams"
    with open(os.path.join(sf,h+".txt"),'w') as f:
        f.write(str(model)+'\n')
        for k in sorted(hyps.keys()):
            f.write(str(k) + ": " + str(hyps[k]) + "\n")
    temp_hyps = dict()
    keys = list(hyps.keys())
    temp_hyps = {k:v for k,v in hyps.items()}
    for k in keys:
        if type(hyps[k]) == type(np.array([])):
            del temp_hyps[k]
    with open(os.path.join(sf,h+".json"),'w') as f:
        json.dump(temp_hyps, f)

def validate_static(hyps, model, data_distr, loss_fn, step_size=500,
                                                     verbose=False):
    """
    Performs validation on non-recurrent (static) models.

    hyps: dict
        dict of relevant hyperparameters
    model: torch nn.Module
        the model to be trained
    data_distr: DataDistributor
        the data distribution object as obtained through
        get_model_and_distr
    loss_fn: loss function
        the loss function
    step_size: int
        optional size of batches when evaluating validation set
    """
    val_preds = []
    val_targs = []
    val_loss = 0
    n_loops = data_distr.val_shape[0]//step_size
    gen = data_distr.val_sample(step_size)
    for i, (val_x, val_y) in enumerate(gen):
        outs = model(val_x.to(DEVICE)).detach()
        val_preds.append(outs.cpu().detach().numpy())
        val_targs.append(val_y.cpu().detach().numpy())
        val_loss += loss_fn(outs, val_y.to(DEVICE)).item()
        if hyps['l1'] > 0:
            n = outs.shape[0]
            vl = hyps['l1'] * torch.norm(outs, 1).float()/n
            val_loss += vl.item()
        if verbose and i%max((n_loops//10),1) == 0:
            n = data_distr.val_y.shape[0]
            print("{}/{}".format(i*step_size,n), end="     \r")
        if hyps['exp_name']=="test" and i >= 3:
            break
    return val_loss, val_preds, val_targs

def get_model_and_distr(hyps, train_data):
    """
    Creates and returns the model and data distributor objects.

    hyps: dict
        dict of relevant hyperparameters
    train_data: DataContainer
        a DataContainer of the training data as returned by get_data
    """
    model = globals()[hyps['model_type']](**hyps)
    model = model.to(DEVICE)
    batch_size = hyps['batch_size']
    seq_len = 1
    shift_labels = utils.try_key(hyps,'shift_labels',False)
    zscorey = utils.try_key(hyps,'zscorey',False)
    cross_val_idx = hyps.get('cross_val_idx', None)
    n_cv_folds = hyps.get('n_cv_folds', 10)
    rand_sample = hyps.get("rand_sample",None)
    data_distr = DataDistributor(train_data, batch_size=batch_size,
                                    shuffle=hyps.get('shuffle',True),
                                    rand_sample=rand_sample,
                                    cross_val_idx=cross_val_idx,
                                    n_cv_folds=n_cv_folds,
                                    recurrent=False,
                                    seq_len=seq_len,
                                    shift_labels=shift_labels,
                                    zscorey=zscorey)
    data_distr.torch()
    return model, data_distr

def static_eval(x, label, model, loss_fn):
    """
    Evaluates non-recurrent models during training.

    x: torch FloatTensor
        a batch of the training data
    label: torch FloatTensor
        a batch of the training labels
    model: torch nn.Module
        the model to be trained
    loss_fn: function
        the loss function. should accept args: (pred, true)
    """
    pred = model(x.to(DEVICE))
    error = loss_fn(pred, label.to(DEVICE))
    return pred,error

def get_data(hyps):
    """
    hyps: dict
        dict of relevant hyperparameters
    """
    cutout_size = None if 'cutout_size' not in hyps else\
                                      hyps['cutout_size']
    img_depth = hyps.get("img_depth", hyps['img_shape'][0])

    if hyps['dataset'] in set(tdrintr.centers.keys()):
        data_path=utils.try_key(hyps,'datapath','~/interneuron_data/')
        train_data = loadintrexpt(expt=hyps['dataset'],
                                  cells=hyps['cells'],
                                  stim_type=hyps['stim_type'],
                                  history=img_depth,
                                  H=None,
                                  W=None,
                                  norm_stats=None,
                                  cutout_width=cutout_size,
                                  data_path=data_path)
    else:
        data_path = utils.try_key(hyps,'datapath','~/experiments/data')
        train_data = loadexpt(hyps['dataset'],hyps['cells'],
                                hyps['stim_type'],'train',img_depth,0,
                                cutout_width=cutout_size,
                                data_path=data_path)
    train_data = DataContainer(train_data)
    norm_stats = [train_data.stats['mean'], train_data.stats['std']]

    try:
        test_data = DataContainer(loadexpt(hyps['dataset'],
                                        hyps['cells'],
                                        hyps['stim_type'],
                                        'test',img_depth,0,
                                        norm_stats=norm_stats,
                                        cutout_width=cutout_size,
                                        data_path=data_path))
    except:
        print("Error loading test data...")
        test_data = None
    hyps['img_shape'] = train_data.X.shape[1:]
    return train_data, test_data

def get_optim_objs(hyps, model, centers=None):
    """
    Returns the optimization objects for the training.

    hyps: dict
        dict of relevant hyperparameters
    model: torch nn.Module
        the model to be trained
    centers: list of tuples or lists, shape: (n_cells, 2)
        the centers of each ganglion cell in terms of image
        coordinates if None centers is ignored
    """
    if 'lossfxn' not in hyps:
        hyps['lossfxn'] = "PoissonNLLLoss"
    if hyps['lossfxn'] == "PoissonNLLLoss" and 'log_poisson' in hyps:
        log_p = hyps['log_poisson']
        loss_fn = globals()[hyps['lossfxn']](log_input=log_p)
    else:
        loss_fn = globals()[hyps['lossfxn']]()

    optimizer = torch.optim.Adam(model.parameters(), lr=hyps['lr'],
                                           weight_decay=hyps['l2'])
    if hyps['scheduler'] == 'ReduceLROnPlateau':
        scheduler = globals()[hyps['scheduler']](optimizer, 'min',
                                  factor=hyps['scheduler_scale'],
                                  patience=hyps['scheduler_patience'],
                                  threshold=hyps['scheduler_thresh'],
                                  verbose=True)
    elif hyps['scheduler'] == 'MultiStepLR':
        milestones = utils.try_key(hyps,'scheduler_milestones',[10,20,30])
        print(milestones)
        scheduler = globals()[hyps['scheduler']](optimizer, milestones,
                                        gamma=0.1)
    else:
        hyps['scheduler'] = "NullScheduler"
        scheduler = NullScheduler()

    return optimizer, scheduler, loss_fn

def fill_hyper_q(hyps, hyp_ranges, keys, hyper_q, idx=0):
    """
    Recursive function to load each of the hyperparameter combinations
    onto a queue.

    hyps - dict of hyperparameters created by a HyperParameters object
        type: dict
        keys: name of hyperparameter
        values: value of hyperparameter
    hyp_ranges - dict of lists
        these are the ranges that will change the hyperparameters for
        each search
        type: dict
        keys: name of hyperparameters to be searched over
        values: list of values to search over for that hyperparameter
    keys - keys of the hyperparameters to be searched over. Used to
            specify order of keys to search
    train - method that handles training of model. Should return a
        dict of results.
    hyper_q - Queue to hold all parameter sets
    idx - the index of the current key to be searched over

    Returns:
        hyper_q: Queue of dicts `hyps`
    """
    # Base call, runs the training and saves the result
    if idx >= len(keys):
        # Ensure necessary hyps are present
        hyps['n_repeats'] = utils.try_key(hyps, 'n_repeats', 1)
        hyps['prune'] = utils.try_key(hyps, 'prune', False)
        hyps['altn_layers'] = utils.try_key(hyps, 'altn_layers', False)
        hyps['abssum'] = utils.try_key(hyps, 'abssum', False)
        hyps['prune_layers'] = utils.try_key(hyps, 'prune_layers', [])
        hyps['prune_intvl'] = utils.try_key(hyps, 'prune_intvl', None)
        hyps['prune_tolerance'] = utils.try_key(hyps,
                                                'prune_tolerance',
                                                0.01)
        if hyps['prune_intvl'] is None:
            hyps['prune_intvl'] = hyps['n_epochs']
        hyps['alpha_steps'] = utils.try_key(hyps, 'alpha_steps', 5)
        hyps['intg_bsize'] = utils.try_key(hyps, 'intg_bsize', 500)
        hyps['scheduler'] = utils.try_key(hyps, 'scheduler',
                                           'ReduceLROnPlateau')
        hyps['scheduler_thresh'] = utils.try_key(hyps,
                                           'scheduler_thresh', 1e-2)
        hyps['scheduler_patience']=utils.try_key(hyps,
                                           'scheduler_patience', 10)
        hyps['scheduler_scale'] = utils.try_key(hyps,
                                           'scheduler_scale', 0.5)
        hyps['cross_val'] = utils.try_key(hyps, 'cross_val', False)
        hyps['cross_val_idx'] = utils.try_key(hyps, 'cross_val_idx',
                                                              0)
        hyps['n_cv_folds'] = utils.try_key(hyps, 'n_cv_folds', 10)
        for i in range(hyps['n_repeats']):
            # Load q
            hyps['search_keys'] = ""
            for k in keys:
                v = str(hyps[k])
                if k == "startpt" and hyps[k] is not None:
                    cp = tdrio.load_checkpoint(v)
                    v = str(cp['exp_name'])+str(cp['exp_num'])
                hyps['search_keys'] += "_" + str(k) + v
            hyper_q.put({k:v for k,v in hyps.items()})

    # Non-base call. Sets a hyperparameter to a new search value and
    # passes down the dict.
    else:
        key = keys[idx]
        for param in hyp_ranges[key]:
            hyps[key] = param
            hyper_q = fill_hyper_q(hyps, hyp_ranges, keys, hyper_q,
                                                             idx+1)
    return hyper_q


def fit_sta(test_chunk, chunked_data, normalize=True):
    """
    Calculates the STA from the chunked data and returns a cpu torch
    tensor of the STA. This function is mainly used for creating
    figures in deepretina paper.

    test_chunk: int
        the held out test data index
    chunked_data: ChunkedData object (see datas.py)
        An object to handle chunking the data for cross validation
    """
    if normalize:
        norm_stats = chunked_data.get_norm_stats(test_chunk)
        mean, std = norm_stats
    n_samples = 0
    cumu_sum = 0
    batch_size = 500
    for i in range(chunked_data.n_chunks):
        if i != test_chunk:
            x = chunked_data.X[chunked_data.chunks[i]]
            y = chunked_data.y[chunked_data.chunks[i]]
            for j in range(0,len(x),batch_size):
                temp_x = x[j:j+batch_size]
                if normalize:
                    temp_x = (temp_x-mean)/(std+1e-5)
                matmul = torch.einsum("ij,i->j",temp_x.to(DEVICE),
                                     y[j:j+batch_size].to(DEVICE))
                cumu_sum = cumu_sum + matmul.cpu()
                n_samples += len(temp_x)
    if normalize:
        return cumu_sum/n_samples, norm_stats
    return cumu_sum/n_samples

def fit_nonlin(chunked_data, test_chunk, model, degree=5,
                                            n_repeats=10,
                                            ret_all=False):
    """
    Fits a polynomial nonlinearity to the model.

    chunked_data - ChunkedData object (see datas.py)
    test_chunk - int
        the chunk of data that will be held for testing
    model - RevCorLN object (see models.py)
    degree - int
        degree of polynomial to fit
    n_repeats - int
        number of repeated trials to fit the nonlinearity
    ret_all - bool
        will return multiple fields if true
    """
    if type(degree)==type(int()):
        degree=[degree]
    best_r = -1
    try:
        X,y = chunked_data.get_train_data(test_chunk)
        X = model.normalize(X)
        lin_outs = model.convolve(X)
    except:
        batch_size = 500
        n_samples = np.sum([len(chunk) if i != test_chunk else 0 for\
                          i,chunk in enumerate(chunked_data.chunks)])
        lin_outs = torch.empty(n_samples).float()
        truth = torch.empty(n_samples).float()
        outdx = 0
        for i in range(chunked_data.n_chunks):
            if i != test_chunk:
                x = chunked_data.X[chunked_data.chunks[i]]
                x = model.normalize(x)
                y = chunked_data.y[chunked_data.chunks[i]]
                for j in range(0,len(x),batch_size):
                    temp_x = x[j:j+batch_size]
                    outs = model.convolve(temp_x).cpu()
                    lin_outs[outdx:outdx+len(outs)] = outs
                    truth[outdx:outdx+len(outs)] = y[j:j+batch_size]
                    outdx += len(outs)
        y = truth

    for d in degree:
        lin_outs = lin_outs.numpy().astype(np.float32).squeeze()
        y = y.numpy().astype(np.float32).squeeze()
        fit = np.polyfit(lin_outs, y, d)
        poly = utils.poly1d(fit)
        preds = poly(lin_outs)
        r = utils.pearsonr(preds, y)
        if r > best_r:
            best_poly = poly
            best_degree=d
            best_r = r
            best_preds = preds

    if ret_all:
        return best_poly, best_degree, best_r, best_preds
    return best_poly

def fit_ln_nonlin(X, y, model, degree=5, fit_size=None,ret_all=False):
    """
    Fits a polynomial nonlinearity to the model.

    X: torch tensor (T, C) or (T, C, H, W)
    y: torch tensor
    model: RevCorLN object (see models.py)
    degree: int
        degree of polynomial to fit
    ret_all: bool
        will return multiple fields if true
    """
    if type(degree)==type(int()):
        degree=[degree]
    best_r = -1
    try:
        X = model.normalize(X)
        lin_outs = model.convolve(X)
    except:
        batch_size = 500
        lin_outs = torch.empty(len(X)).float()
        for i in range(0, len(X), batch_size):
            x = X[i:i+batch_size]
            x = model.normalize(x)
            outs = model.convolve(x).cpu().detach()
            lin_outs[i:i+batch_size] = outs

    lin_outs = lin_outs.numpy().astype(np.float32).squeeze()
    y = y.numpy().astype(np.float32).squeeze()
    if isinstance(fit_size, int) and fit_size < len(lin_outs):
        sample = np.random.randint(0,len(lin_outs),
                                         fit_size).astype(np.int)
        lin_outs = lin_outs[sample]
        y = y[sample]

    for d in degree:
        fit = np.polyfit(lin_outs, y, d)
        poly = utils.poly1d(fit)
        preds = poly(lin_outs)
        r = utils.pearsonr(preds, y)
        if r > best_r:
            best_poly = poly
            best_degree=d
            best_r = r
            best_preds = preds
    if ret_all:
        return best_poly, best_degree, best_r, best_preds
    return best_poly

def train_ln(X, y, rf_center, cutout_size,verbose=True):
    """
    Fits an LN model to the data

    X: torch FloatTensor (T,C) or (T,C,H,W)
        training stimulus
    y: torch FloatTensor (T)
    rf_center: list or tuple (row, col)
        the receptive field center of the cell
    cutout_size: int
        stimulus is cutout centered at argued center
    """
    if type(X) == type(np.array([])):
        X = torch.FloatTensor(X)
    if type(y) == type(np.array([])):
        y = torch.FloatTensor(y)

    # STA is cpu torch tensor
    if verbose:
        print("Performing reverse correlation")
    sta, norm_stats, _ = utils.revcor(X, y, batch_size=500,
                                          ret_norm_stats=True)
    model = RevCorLN(sta.reshape(-1),ln_cutout_size=cutout_size,
                                               center=rf_center,
                                               norm_stats=norm_stats)
    if verbose:
        print("Fitting nonlinearity")
    bests = fit_ln_nonlin(X, y, model,degree=[5], fit_size=15000,
                                                    ret_all=True)
    best_poly,best_degree,best_r,best_preds = bests
    model.poly = best_poly # Torch compatible polys
    model.poly_degree = best_degree

    return model


def cross_validate_ln(chunked_data, ln_cutout_size, center,
                                           ret_models=True,
                                           skip_chunks={},
                                           verbose=True):
    """
    Performs cross validation for LN model trained using reverse
    correlation

    chunked_data: ChunkedData object (see datas.py)
        This is an object that segregates the data into N distinct
        chunks
    ln_cutout_size: int
        the the size of the window to train on the stimulus
    center: list or tuple of ints (row,col)
        the center coordinate of the receptive field for the cell
    ret_models: bool
        if true, the models are each collected and returned at the
        end of the cross validation
    skip_chunks: set or list
        chunk indices to skip during training
    """
    accs = []
    models = []
    if verbose:
        print("Fitting LNs...")
    for i in range(chunked_data.n_chunks):
        basetime=time.time()

        # STA is cpu torch tensor
        sta,norm_stats = fit_sta(i,chunked_data,normalize=True)

        model = RevCorLN(sta,ln_cutout_size=ln_cutout_size,
                                             center=center,
                                             norm_stats=norm_stats)
        bests = fit_nonlin(chunked_data,i,model,degree=[5],
                                              ret_all=True)
        best_poly,best_degree,best_r,best_preds = bests
        model.poly = best_poly # Torch compatible polys
        model.poly_degree = best_degree

        val_X = chunked_data.X[chunked_data.chunks[i]]
        val_y = chunked_data.y[chunked_data.chunks[i]]
        val_X = model.normalize(val_X)
        preds = model(val_X).squeeze()
        r = utils.pearsonr(preds.squeeze(), val_y.squeeze()).item()
        accs.append(r)
        if ret_models:
            models.append(model)
        exec_time = time.time()-basetime
        if verbose:
            s = "Fit Trial {}, Best Degree: {}, Acc: {}, Time: {}"
            s = s.format(i, best_degree, r, exec_time)
            print(s)
        del val_X
        del val_y
    return models, accs
