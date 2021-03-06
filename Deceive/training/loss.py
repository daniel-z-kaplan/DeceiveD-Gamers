# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.
from tornado.log import gen_log

"""Loss functions."""

import numpy as np
import torch
from torch_utils import training_stats
from torch_utils import misc
from torch_utils.ops import conv2d_gradfix

#----------------------------------------------------------------------------

class Loss:
    def accumulate_gradients(self, phase, real_img, real_c, gen_z, gen_c, sync, gain): # to be overridden by subclass
        raise NotImplementedError()

#----------------------------------------------------------------------------

class StyleGAN2Loss(Loss):
    def __init__(self, device, G_mapping, G_synthesis, D, augment_pipe=None, style_mixing_prob=0.9, r1_gamma=10, pl_batch_shrink=2, pl_decay=0.01, pl_weight=2, with_dataaug=False, k = 100):
        super().__init__()
        self.device = device
        self.G_mapping = G_mapping
        self.G_synthesis = G_synthesis
        self.D = D
        self.augment_pipe = augment_pipe
        self.style_mixing_prob = style_mixing_prob
        self.r1_gamma = r1_gamma
        self.pl_batch_shrink = pl_batch_shrink
        self.pl_decay = pl_decay
        self.pl_weight = pl_weight
        self.pl_mean = torch.zeros([], device=device)
        self.with_dataaug = with_dataaug
        self.pseudo_data = None
        self.G_score = torch.tensor(500.0, requires_grad = False)
        self.D_score = torch.tensor(500.0, requires_grad = False)
        torch.autograd.set_detect_anomaly(True)
        self.k = k

    def run_G(self, z, c, sync):
        with misc.ddp_sync(self.G_mapping, sync):
            ws = self.G_mapping(z, c)
            if self.style_mixing_prob > 0:
                with torch.autograd.profiler.record_function('style_mixing'):
                    cutoff = torch.empty([], dtype=torch.int64, device=ws.device).random_(1, ws.shape[1])
                    cutoff = torch.where(torch.rand([], device=ws.device) < self.style_mixing_prob, cutoff, torch.full_like(cutoff, ws.shape[1]))
                    ws[:, cutoff:] = self.G_mapping(torch.randn_like(z), c, skip_w_avg_update=True)[:, cutoff:]
        with misc.ddp_sync(self.G_synthesis, sync):
            img = self.G_synthesis(ws)
        return img, ws

    def run_D(self, img, c, sync):
        # Enable standard data augmentations when --with-dataaug=True
        if self.with_dataaug and self.augment_pipe is not None:
            img = self.augment_pipe(img)
        with misc.ddp_sync(self.D, sync):
            logits = self.D(img, c)
        return logits

    def adaptive_pseudo_augmentation(self, real_img):
        # Apply Adaptive Pseudo Augmentation (APA)
        batch_size = real_img.shape[0]
        pseudo_flag = torch.ones([batch_size, 1, 1, 1], device=self.device)
        pseudo_flag = torch.where(torch.rand([batch_size, 1, 1, 1], device=self.device) < self.augment_pipe.p,
                                  pseudo_flag, torch.zeros_like(pseudo_flag))
        if torch.allclose(pseudo_flag, torch.zeros_like(pseudo_flag)):
            return real_img
        else:
            assert self.pseudo_data is not None
            return self.pseudo_data * pseudo_flag + real_img * (1 - pseudo_flag)

    def accumulate_gradients(self, phase, real_img, real_c, gen_z, gen_c, sync, gain):
        assert phase in ['Gmain', 'Greg', 'Gboth', 'Dmain', 'Dreg', 'Dboth']
        do_Gmain = (phase in ['Gmain', 'Gboth'])
        do_Dmain = (phase in ['Dmain', 'Dboth'])
        do_Gpl   = (phase in ['Greg', 'Gboth']) and (self.pl_weight != 0)
        do_Dr1   = (phase in ['Dreg', 'Dboth']) and (self.r1_gamma != 0)
        
        #k is max score, or base score? Base score is 1, max score is... 24
        #So for a base of 500/500, this means that if the logit is .6, our ratio of G/(G+D) = 6
        #Adjust towards that, up to K. 

        def adjust_score(logits):
            
            k = self.k
            print("Received logits", logits)
            print('G_score', self.G_score)
            print('D_score', self.D_score)

            mean = torch.mean(logits).detach()
            print("Logits mean:", mean)
            change = torch.sub(torch.divide(torch.divide(self.G_score, self.D_score),2), mean) #So when scaling is .5 and mean is .6, discriminator is doing better than expected. Change = -.1, times K. Let's try this for now-ish..
            #.5 and .6, change = -.1, change * k = -2.4. G gains 2.4, D loses 2.4
            #.5 and .4, change is .1, change * k = 2.4. G loses 2.4, D gains 2.4
            #So we use change * k as the factor to change by
            #But it shouldn't keep going up when elo difference is high...?
            #We should use a higher BASE value, but actually have it stop going up as it goes up
            #It's a catchup mechanism
            print("Change factor:",change)
            print("Should change by this much:", torch.mul(change,k))
            self.G_score = torch.sub(self.G_score,torch.mul(change, k))
            self.D_score = torch.add(self.D_score,torch.mul(change, k))
            print('G_score', self.G_score)
            print('D_score', self.D_score)
            print("Adjusted scaling:",self.G_score/self.D_score)
        
        
        #So it does A, then B, then C/D, then part of D
        
        #So what happened id generator started getting higher than D. 
        #We scale loss by the ratio. So if G is higher than D, we send MORE loss.
        #
        
        gen_logits_t = []
        
        #D aims to predict 0's, or fakes. Because this is how G is doing, we invert it.
        # Gmain: Maximize logits for generated images.
        if do_Gmain:
            with torch.autograd.profiler.record_function('Gmain_forward'):
                gen_img, _gen_ws = self.run_G(gen_z, gen_c, sync=(sync and not do_Gpl)) # May get synced by Gpl.
                # Update pseudo data
                self.pseudo_data = gen_img.detach()
                gen_logits = self.run_D(gen_img, gen_c, sync=False)
                print("Maximize logits for generated")
                adjust_score(torch.sigmoid(gen_logits))
                training_stats.report('Loss/scores/fake', gen_logits)
                training_stats.report('Loss/signs/fake', gen_logits.sign())
                loss_Gmain = torch.nn.functional.softplus(-gen_logits) # -log(sigmoid(gen_logits))
                training_stats.report('Loss/G/loss', loss_Gmain)
            with torch.autograd.profiler.record_function('Gmain_backward'):
                loss_Gmain.mean().mul(gain).backward()

        # Gpl: Apply path length regularization.
        if do_Gpl:
            with torch.autograd.profiler.record_function('Gpl_forward'):
                batch_size = gen_z.shape[0] // self.pl_batch_shrink
                gen_img, gen_ws = self.run_G(gen_z[:batch_size], gen_c[:batch_size], sync=sync)
                pl_noise = torch.randn_like(gen_img) / np.sqrt(gen_img.shape[2] * gen_img.shape[3])
                with torch.autograd.profiler.record_function('pl_grads'), conv2d_gradfix.no_weight_gradients():
                    pl_grads = torch.autograd.grad(outputs=[(gen_img * pl_noise).sum()], inputs=[gen_ws], create_graph=True, only_inputs=True)[0]
                pl_lengths = pl_grads.square().sum(2).mean(1).sqrt()
                pl_mean = self.pl_mean.lerp(pl_lengths.mean(), self.pl_decay)
                self.pl_mean.copy_(pl_mean.detach())
                pl_penalty = (pl_lengths - pl_mean).square()
                training_stats.report('Loss/pl_penalty', pl_penalty)
                loss_Gpl = pl_penalty * self.pl_weight
                training_stats.report('Loss/G/reg', loss_Gpl)
            with torch.autograd.profiler.record_function('Gpl_backward'):
                (gen_img[:, 0, 0, 0] * 0 + loss_Gpl).mean().mul(gain).backward()


        
        # Dmain: Minimize logits for generated images.
        loss_Dgen = 0
        if do_Dmain:
            with torch.autograd.profiler.record_function('Dgen_forward'):
                gen_img, _gen_ws = self.run_G(gen_z, gen_c, sync=False)
                gen_logits = self.run_D(gen_img, gen_c, sync=False) # Gets synced by loss_Dreal.
                print("Minimize logits for generated via D")
                adjust_score(torch.sigmoid(gen_logits))#Send as is, because they wanna be zero.
                training_stats.report('Loss/scores/fake', gen_logits)
                training_stats.report('Loss/signs/fake', gen_logits.sign())
                loss_Dgen = torch.nn.functional.softplus(gen_logits) # -log(1 - sigmoid(gen_logits))
            with torch.autograd.profiler.record_function('Dgen_backward'):
                scaling = torch.div(self.G_score, self.D_score).to(self.device)
                loss_Dgen.mean().mul(gain).mul(scaling).backward()#Changed now
#                 loss_Dgen.mean().mul(gain).backward()         
        
        
        # Dmain: Maximize logits for real images.
        # Dr1: Apply R1 regularization.
        if do_Dmain or do_Dr1:
            name = 'Dreal_Dr1' if do_Dmain and do_Dr1 else 'Dreal' if do_Dmain else 'Dr1'
            with torch.autograd.profiler.record_function(name + '_forward'):
                # Apply Adaptive Pseudo Augmentation (APA) when --aug!='noaug'
                if self.augment_pipe is not None:
                    real_img_tmp = self.adaptive_pseudo_augmentation(real_img)
                else:
                    real_img_tmp = real_img
                real_img_tmp = real_img_tmp.detach().requires_grad_(do_Dr1)
                real_logits = self.run_D(real_img_tmp, real_c, sync=sync)
                training_stats.report('Loss/scores/real', real_logits)
                training_stats.report('Loss/signs/real', real_logits.sign())
                print("Maximize logits for real via D")
                adjust_score(torch.sigmoid(1-real_logits))#Invert real logits, because they are meant to be 1
                loss_Dreal = 0
                if do_Dmain:
                    loss_Dreal = torch.nn.functional.softplus(-real_logits) # -log(sigmoid(real_logits))
                    training_stats.report('Loss/D/loss', loss_Dgen + loss_Dreal)

                loss_Dr1 = 0
                if do_Dr1:
                    with torch.autograd.profiler.record_function('r1_grads'), conv2d_gradfix.no_weight_gradients():
                        r1_grads = torch.autograd.grad(outputs=[real_logits.sum()], inputs=[real_img_tmp], create_graph=True, only_inputs=True)[0]
                    r1_penalty = r1_grads.square().sum([1,2,3])
                    loss_Dr1 = r1_penalty * (self.r1_gamma / 2)
                    training_stats.report('Loss/r1_penalty', r1_penalty)
                    training_stats.report('Loss/D/reg', loss_Dr1)

            with torch.autograd.profiler.record_function(name + '_backward'):
                scaling = torch.div(self.G_score, self.D_score).to(self.device)
                (real_logits * 0 + loss_Dreal + loss_Dr1).mean().mul(gain).mul(scaling).backward()#Changed here
#                 (real_logits * 0 + loss_Dreal + loss_Dr1).mean().mul(gain).backward()#Changed here
                
          
                #So when we are here, we have gen_logits_t
        #We can average both the gen_logits.
        #Then we can average them again.
        #The ratio of Gscore/(DScore+GScore) = .5 is for equal...?
        #When DScore gets higher than GScore, the stuff and things. Let's say 100 points= 10%
        #Base scores of 500?
        #So 60% = 600/1000
        #Basically we wanna move towards this by K every time, where K is a max of... 25?
        

        #Maybe a light reinforcement learning?
#----------------------------------------------------------------------------
