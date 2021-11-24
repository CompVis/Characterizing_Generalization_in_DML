import numpy as np, copy
import torch, torch.nn as nn, torch.nn.functional as F
import batchminer as bmine
import criteria

"""================================================================================================="""
ALLOWED_MINING_OPS  = None
REQUIRES_BATCHMINER = False
REQUIRES_OPTIM      = True


class Criterion(torch.nn.Module):
    def __init__(self, opt):
        """
        Args:
            opt: Namespace containing all relevant parameters.
        """
        super(Criterion, self).__init__()

        self.opt = opt

        #### Some base flags and parameters
        self.ALLOWED_MINING_OPS  = ALLOWED_MINING_OPS
        self.REQUIRES_BATCHMINER = REQUIRES_BATCHMINER
        self.REQUIRES_OPTIM      = REQUIRES_OPTIM
        self.name           = 'S2SD'
        self.d_mode         = 'cosine'
        self.iter_count     = 0
        self.embed_dim      = opt.embed_dim

        ### Will contain all parameters to be optimized, e.g. the target MLPs and
        ### potential parameters of training criteria.
        self.optim_dict_list = []

        ### All S2SD-specific Parameters
        self.T      = opt.loss_s2sd_T
        self.w      = opt.loss_s2sd_w
        self.feat_w = opt.loss_s2sd_feat_w
        self.pool_aggr     = opt.loss_s2sd_pool_aggr
        self.match_feats   = opt.loss_s2sd_feat_distill
        self.max_feat_iter = opt.loss_s2sd_feat_distill_delay

        ### Initialize all target networks as two-layer MLPs
        if 'resnet50' in opt.arch:
            f_dim = 2048
        elif 'resnet18' in opt.arch:
            f_dim = 512
        elif 'bninception' in opt.arch:
            f_dim = 1024
        elif 'efficient' in opt.arch:
            f_dim = 1280
        else:
            f_dim = 2048

        self.target_nets  = torch.nn.ModuleList([nn.Sequential(nn.Linear(f_dim, t_dim), nn.ReLU(), nn.Linear(t_dim, t_dim)) for t_dim in opt.loss_s2sd_target_dims])
        self.optim_dict_list.append({'params':self.target_nets.parameters(), 'lr':opt.lr})

        ### Initialize all target criteria. As each criterion may require its separate set of
        ### trainable parameters, several instances have to be created.
        old_embed_dim = copy.deepcopy(opt.embed_dim)
        self.target_criteria = nn.ModuleList()
        for t_dim in opt.loss_s2sd_target_dims:
            opt.embed_dim = t_dim

            batchminer       = bmine.select(opt.batch_mining, opt)
            target_criterion = criteria.select(opt.loss_s2sd_target, opt, batchminer=batchminer)
            self.target_criteria.append(target_criterion)

            if hasattr(target_criterion, 'optim_dict_list'):
                self.optim_dict_list.extend(target_criterion.optim_dict_list)
            else:
                self.optim_dict_list.append({'params':target_criterion.parameters(), 'lr':opt.lr})

        ### Initialize the source objective. By default the same as the target objective(s)
        opt.embed_dim = old_embed_dim
        batchminer   = bmine.select(opt.batch_mining, opt)
        self.source_criterion = criteria.select(opt.loss_s2sd_source, opt, batchminer=batchminer)

        if hasattr(self.source_criterion, 'optim_dict_list'):
            self.optim_dict_list.extend(self.source_criterion.optim_dict_list)
        else:
            self.optim_dict_list.append({'params':self.source_criterion.parameters(), 'lr':opt.lr})



    def prep(self, thing):
        return 1.*torch.nn.functional.normalize(thing, dim=1)


    def forward(self, batch, labels, batch_features, avg_batch_features, f_embed, **kwargs):
        """
        Args:
            batch:   torch.Tensor: Input of embeddings with size (BS x DIM)
            labels: nparray/list: For each element of the batch assigns a class [0,...,C-1], shape: (BS x 1)
        """
        ###
        bs          = len(batch)
        batch       = self.prep(batch)
        self.labels = labels.unsqueeze(1)

        ### Compute loss on base/source embedding space as well as the similarity matrix of all base embeddings in <batch>.
        source_loss = self.source_criterion(batch, labels, batch_features=batch_features, f_embed=f_embed, **kwargs)
        source_smat = self.smat(batch, batch, mode=self.d_mode)
        loss        = source_loss

        ### If required, use combined global max- and average pooling to produce the feature space.
        if self.pool_aggr:
            avg_batch_features = nn.AdaptiveAvgPool2d(1)(batch_features).view(bs,-1)+nn.AdaptiveMaxPool2d(1)(batch_features).view(bs,-1)
        else:
            avg_batch_features = avg_batch_features.view(bs,-1)

        ### Key Segment (1): For each target branch, computes the respective loss <target_loss> and similarity matrix <target_smat>.
        ### These will be used as distillation signal by computing the KL-Divergence to the source similarity matrix <source_smat>.
        kl_divs, target_losses  = [], []
        for i,out_net in enumerate(self.target_nets):
            target_batch   = F.normalize(out_net(avg_batch_features.view(bs, -1)), dim=-1)
            target_loss    = self.target_criteria[i](target_batch, labels, batch_features=batch_features, f_embed=f_embed, **kwargs)
            target_smat    = self.smat(target_batch, target_batch, mode=self.d_mode)

            kl_divs.append(self.kl_div(source_smat, target_smat.detach()))
            target_losses.append(target_loss)

        loss = (torch.mean(torch.stack(target_losses)) + loss)/2. + self.w*torch.mean(torch.stack(kl_divs))

        ### If enough iterations have passed, start applying feature space distillation to bridge the
        ### dimensionality bottleneck.
        if self.match_feats and self.iter_count>=self.max_feat_iter:
            n_avg_batch_features = F.normalize(avg_batch_features, dim=-1).detach()
            avg_feat_smat        = self.smat(n_avg_batch_features, n_avg_batch_features, mode=self.d_mode)
            avg_batch_kl_div     = self.kl_div(source_smat, avg_feat_smat.detach())
            loss += self.feat_w*avg_batch_kl_div

        ### Update iteration counter for every training iteration.
        self.iter_count+=1

        return loss



    ### Apply relation distillation over similiarity vectors.
    def kl_div(self, A, B):
        log_p_A = F.log_softmax(A/self.T, dim=-1)
        p_B     = F.softmax(B/self.T, dim=-1)
        kl_div  = F.kl_div(log_p_A, p_B, reduction='sum') * (self.T**2) / A.shape[0]
        return kl_div


    ### Computes similarity matrices.
    def smat(self, A, B, mode='cosine'):
        if mode=='cosine':
            return A.mm(B.T)
        elif mode=='euclidean':
            As, Bs = A.shape, B.shape
            return (A.mm(A.T).diag().unsqueeze(-1)+B.mm(B.T).diag().unsqueeze(0)-2*A.mm(B.T)).clamp(min=1e-20).sqrt()
