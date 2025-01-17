import numpy as np
import pandas as pd
from tqdm import tqdm as tqdm

import os
import json
import torch
import torch.nn as nn
import torch.nn.functional as F

from core.attacks import create_attack
from core.metrics import accuracy
from core.models import create_model

from .context import ctx_noparamgrad_and_eval
from .utils import seed

from .mart import mart_loss
from .rst import CosineLR
from .trades import trades_loss
# from mc_score import MC_Score_EDM
from ..data import SCORE_DATASETS, SEMISUP_DATASETS


device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

SCHEDULERS = ['cyclic', 'step', 'cosine', 'cosinew']


class Trainer(object):
    """
    Helper class for training a deep neural network.
    Arguments:
        info (dict): dataset information.
        args (dict): input arguments.
    """
    def __init__(self, info, args):
        super(Trainer, self).__init__()
        
        seed(args.seed)
        self.model = create_model(args.model, args.normalize, info, device)

        self.params = args
        self.criterion = nn.CrossEntropyLoss()
        self.init_optimizer(self.params.num_adv_epochs)
        
        if self.params.pretrained_file is not None:
            self.load_model(os.path.join(self.params.log_dir, self.params.pretrained_file, 'weights-best.pt'))
        
        self.attack, self.eval_attack = self.init_attack(self.model, self.criterion, self.params.attack, self.params.attack_eps, 
                                                         self.params.attack_iter, self.params.attack_step)

        # if self.params.score: # initalize a score model
        #     assert isinstance(self.params.time, float)
        #     assert isinstance(self.params.n_mc_samples, int)
        #     score_model = MC_Score_EDM(self.params.score_network_pkl, self.params.time, self.params.n_mc_samples, self.params.n_chunks)
        #     self.score_model = nn.DataParallel(score_model).to(device)

        
    
    @staticmethod
    def init_attack(model, criterion, attack_type, attack_eps, attack_iter, attack_step):
        """
        Initialize adversary.
        """
        attack = create_attack(model, criterion, attack_type, attack_eps, attack_iter, attack_step, rand_init_type='uniform')
        if attack_type in ['linf-pgd', 'l2-pgd']:
            eval_attack = create_attack(model, criterion, attack_type, attack_eps, 2*attack_iter, attack_step)
        elif attack_type in ['fgsm', 'linf-df']:
            eval_attack = create_attack(model, criterion, 'linf-pgd', 8/255, 20, 2/255)
        elif attack_type in ['fgm', 'l2-df']:
            eval_attack = create_attack(model, criterion, 'l2-pgd', 128/255, 20, 15/255)
        return attack,  eval_attack
    
    
    def init_optimizer(self, num_epochs):
        """
        Initialize optimizer and scheduler.
        """
        self.optimizer = torch.optim.SGD(self.model.parameters(), lr=self.params.lr, weight_decay=self.params.weight_decay, 
                                         momentum=0.9, nesterov=self.params.nesterov)
        if num_epochs <= 0:
            return
        self.init_scheduler(num_epochs)
    
        
    def init_scheduler(self, num_epochs):
        """
        Initialize scheduler.
        """
        if self.params.scheduler == 'cyclic':
            num_samples = 50000 if 'cifar10' in self.params.data else 73257
            num_samples = 100000 if 'tiny-imagenet' in self.params.data else num_samples
            update_steps = int(np.floor(num_samples/self.params.batch_size) + 1)
            self.scheduler = torch.optim.lr_scheduler.OneCycleLR(self.optimizer, max_lr=self.params.lr, pct_start=0.25,
                                                                 steps_per_epoch=update_steps, epochs=int(num_epochs))
        elif self.params.scheduler == 'step':
            self.scheduler = torch.optim.lr_scheduler.MultiStepLR(self.optimizer, gamma=0.1, milestones=[100, 105])    
        elif self.params.scheduler == 'cosine':
            self.scheduler = CosineLR(self.optimizer, max_lr=self.params.lr, epochs=int(num_epochs))
        elif self.params.scheduler == 'cosinew':
            self.scheduler = torch.optim.lr_scheduler.OneCycleLR(self.optimizer, max_lr=self.params.lr, pct_start=0.025, 
                                                                 total_steps=int(num_epochs))
        else:
            self.scheduler = None
    
    
    def train(self, dataloader, epoch=0, adversarial=False, verbose=False):
        """
        Run one epoch of training.
        """
        metrics = pd.DataFrame()
        self.model.train()
        
        for data in tqdm(dataloader, desc='Epoch {}: '.format(epoch), disable=not verbose):

            if self.params.data in SEMISUP_DATASETS:
                x, y, p = data
                if self.params.standard_pseudo:
                    x_pseudo = x[p].to(device)
                    y_pseudo = y[p].to(device)
                    p_not = torch.logical_not(p)
                    x = x[p_not]
                    y = y[p_not]
            else:
                x, y = data

            x, y = x.to(device), y.to(device)

            y_x_score = None
            if self.params.data in SCORE_DATASETS:
                x, y_x_score = x.chunk(2, dim=1)
            
            if adversarial:
                
                if self.params.score_matching:
                    x.requires_grad_(True)
                    loss, batch_metrics = self.standard_loss(x, y)
                    # score matching loss here
                else:
                    if self.params.beta is not None and self.params.mart:
                        loss, batch_metrics = self.mart_loss(x, y, beta=self.params.beta)
                    elif self.params.beta is not None:
                        loss, batch_metrics = self.trades_loss(x, y, beta=self.params.beta)
                    else:
                        loss, batch_metrics = self.adversarial_loss(x, y, y_x_score=y_x_score)
            else:
                loss, batch_metrics = self.standard_loss(x, y)

            if self.params.data in SEMISUP_DATASETS and self.params.standard_pseudo:
                p_loss, p_batch_metrics = self.standard_loss(x_pseudo, y_pseudo)
                p_weight = self.params.unsup_fraction
                loss = p_loss*p_weight + loss*(1. - p_weight)
                metrics = pd.concat([metrics, pd.DataFrame(p_batch_metrics, index=[0])], ignore_index=True)
                
            loss.backward(create_graph=self.params.score_matching)

            if self.params.score_matching:
                # x.grad is gradient of NLL w.r.t. x
                sm_loss = 0.5*(y_x_score + x.shape[0]*x.grad).square().view(x.shape[0], -1).sum(dim=-1).mean()
                metrics = pd.concat([metrics, pd.DataFrame({'sm_loss': sm_loss.item()}, index=[0])], ignore_index=True)
                
                (self.params.beta * sm_loss).backward()

                x.grad = None


            if self.params.clip_grad:
                nn.utils.clip_grad_norm_(self.model.parameters(), self.params.clip_grad)
            self.optimizer.step()
            if self.params.scheduler in ['cyclic']:
                self.scheduler.step()
            
            metrics = pd.concat([metrics, pd.DataFrame(batch_metrics, index=[0])], ignore_index=True)
        
        if self.params.scheduler in ['step', 'converge', 'cosine', 'cosinew']:
            self.scheduler.step()
        return dict(metrics.mean())
    
    
    def standard_loss(self, x, y):
        """
        Standard training.
        """
        self.optimizer.zero_grad()
        out = self.model(x)
        loss = self.criterion(out, y)
        
        preds = out.detach()
        batch_metrics = {'loss': loss.item(), 'clean_acc': accuracy(y, preds)}
        return loss, batch_metrics
    
    def adversarial_loss(self, x, y, y_x_score=None):
        """
        Adversarial training (Madry et al, 2017).
        """
        with ctx_noparamgrad_and_eval(self.model):
            x_adv, _ = self.attack.perturb(x, y, y_x_score=y_x_score, gamma=self.params.gamma)
        
        self.optimizer.zero_grad()
        if self.params.keep_clean:
            x_adv = torch.cat((x, x_adv), dim=0)
            y_adv = torch.cat((y, y), dim=0)
        else:
            y_adv = y
        out = self.model(x_adv)
        loss = self.criterion(out, y_adv)

        # loss += gradient_alignment_loss()
        
        preds = out.detach()
        batch_metrics = {'loss': loss.item()}
        if self.params.keep_clean:
            preds_clean, preds_adv = preds[:len(x)], preds[len(x):]
            batch_metrics.update({'clean_acc': accuracy(y, preds_clean), 'adversarial_acc': accuracy(y, preds_adv)})
        else:
            batch_metrics.update({'adversarial_acc': accuracy(y, preds)})    
        return loss, batch_metrics
    
    
    def trades_loss(self, x, y, beta):
        """
        TRADES training.
        """
        loss, batch_metrics = trades_loss(self.model, x, y, self.optimizer, step_size=self.params.attack_step, 
                                          epsilon=self.params.attack_eps, perturb_steps=self.params.attack_iter, 
                                          beta=beta, attack=self.params.attack)
        return loss, batch_metrics  

    
    def mart_loss(self, x, y, beta):
        """
        MART training.
        """
        loss, batch_metrics = mart_loss(self.model, x, y, self.optimizer, step_size=self.params.attack_step, 
                                        epsilon=self.params.attack_eps, perturb_steps=self.params.attack_iter, 
                                        beta=beta, attack=self.params.attack)
        return loss, batch_metrics  
    
    
    def eval(self, dataloader, adversarial=False):
        """
        Evaluate performance of the model.
        """
        acc = 0.0
        self.model.eval()
        
        for batch in dataloader:
            if len(batch) == 3: # quick fix in case eval is called on semisup training dataset
                x, y, _ = batch
            else:
                x, y = batch
            x, y = x.to(device), y.to(device)
            if adversarial:
                with ctx_noparamgrad_and_eval(self.model):
                    x_adv, _ = self.eval_attack.perturb(x, y)            
                out = self.model(x_adv)
            else:
                out = self.model(x)
            acc += accuracy(y, out)
        acc /= len(dataloader)
        return acc

    
    def save_model(self, path):
        """
        Save model weights.
        """
        torch.save({'model_state_dict': self.model.state_dict()}, path)

    
    def load_model(self, path, load_opt=True):
        """
        Load model weights.
        """
        checkpoint = torch.load(path)
        if 'model_state_dict' not in checkpoint:
            raise RuntimeError('Model weights not found at {}.'.format(path))
        self.model.load_state_dict(checkpoint['model_state_dict'])
