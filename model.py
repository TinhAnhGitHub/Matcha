import sys
from pathlib import Path
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers import Pix2StructConfig, Pix2StructForConditionalGeneration,Pix2StructVisionEncoder


class Matcha(nn.Module):
    """
    The Matcha model
    """
    def __init__(self, cfg):
        super().__init__()  # Initialize the nn.Module parent class

        # Initialize Pix2Struct backbone configuration
        backbone_config = Pix2StructConfig.from_pretrained(cfg.model.backbone_path)
        backbone_config.text_config.max_length = cfg.model.max_length
        backbone_config.text_config.is_decoder = True
        backbone_config.text_config.pad_token_id = cfg.model.pad_token_id
        backbone_config.text_config.decoder_start_token_id = cfg.model.decoder_start_token_id
        backbone_config.text_config.bos_token_id = cfg.model.bos_token_id

        # Initialize Pix2Struct model
        self.backbone = Pix2StructForConditionalGeneration.from_pretrained(
            cfg.model.backbone_path,
            config=backbone_config,
        )

        # Load checkpoint if provided
        if cfg.get("from_checkpoint_dir"):
            checkpoint_path = cfg.from_checkpoint_dir  # Path to the .pth file
            print(f"Loading checkpoint from {checkpoint_path}")
            checkpoint = torch.load(checkpoint_path)  # Load the checkpoint file

            # Load model state_dict
            if 'state_dict' in checkpoint:  # If the checkpoint contains a `state_dict`
                self.backbone.load_state_dict(checkpoint['state_dict'], strict=False)
                print("Loaded model state_dict from checkpoint.")
            else:  
                self.backbone.load_state_dict(checkpoint, strict=False)
                print("Loaded raw state_dict into the model.")

            # Optionally load optimizer state_dict if necessary
            if 'optimizer_state_dict' in checkpoint and hasattr(self, 'optimizer'):
                self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
                print("Loaded optimizer state_dict from checkpoint.")

        # Freeze some encoder layers
        print("Freezing the encoder...")
        to_freeze_layer = int(0.4 * backbone_config.num_hidden_layers)
        for layer in self.backbone.encoder.layer[:to_freeze_layer]:
            for param in layer.parameters():
                param.requires_grad = False
        for param in self.backbone.embeddings.parameters():
            param.requires_grad = False

        # for param in self.backbone.encoder.parameters():
            # param.requires_grad = False

        # Resize embeddings for tokenizer length
        print("Resizing model embeddings...")
        print(f"Tokenizer length = {cfg.model.len_tokenizer}")
        self.backbone.decoder.resize_token_embeddings(cfg.model.len_tokenizer)
        print("Finished resizing")

        # Define loss function
        self.loss_fn = nn.CrossEntropyLoss(
            ignore_index=-100,
            reduction="mean",
        )
    def forward(
            self,
            flattened_patches,
            attention_mask,
            labels,
    ):

        outputs = self.backbone(
            flattened_patches=flattened_patches,
            attention_mask=attention_mask,
            labels=labels,
        )

        loss_main = outputs.loss

        # logits = outputs.logits  # [:, 0]  # (bs, vocab)
        # labels_cls = labels[:, 0]  # (bs, )
        # loss = self.loss_fn(logits, labels)

        # logits_num = outputs.logits[:, 1]  # (bs, vocab)
        # labels_num = labels[:, 1]  # (bs, )
        # loss_num = self.loss_fn(logits_num, labels_num)

        loss = loss_main  

        loss_dict = {
            "loss_main": loss_main,
            "loss_cls": loss,
        }

        return loss, loss_dict
    


class AWP:
    """Implements weighted adverserial perturbation
    adapted from: https://www.kaggle.com/code/wht1996/feedback-nn-train/notebook
    """

    def __init__(self, model, optimizer, adv_param="weight", adv_lr=1, adv_eps=0.0001):
        self.model = model
        self.optimizer = optimizer
        self.adv_param = adv_param
        self.adv_lr = adv_lr
        self.adv_eps = adv_eps
        self.backup = {}
        self.backup_eps = {}

    def attack_backward(self, batch, accelerator):
        if self.adv_lr == 0:
            return
        self._save()
        self._attack_step()

        adv_loss, _ = self.model(
            flattened_patches=batch["flattened_patches"],
            attention_mask=batch["attention_mask"],
            labels=batch["labels"]
        )
        self.optimizer.zero_grad()
        accelerator.backward(adv_loss)
        self._restore()

    def _attack_step(self):
        e = 1e-6
        for name, param in self.model.named_parameters():
            if param.requires_grad and param.grad is not None and self.adv_param in name:
                norm1 = torch.norm(param.grad)
                norm2 = torch.norm(param.data.detach())
                if norm1 != 0 and not torch.isnan(norm1):
                    r_at = self.adv_lr * param.grad / (norm1 + e) * (norm2 + e)
                    param.data.add_(r_at)
                    param.data = torch.min(
                        torch.max(param.data, self.backup_eps[name][0]), self.backup_eps[name][1]
                    )

    def _save(self):
        for name, param in self.model.named_parameters():
            if param.requires_grad and param.grad is not None and self.adv_param in name:
                if name not in self.backup:
                    self.backup[name] = param.data.clone()
                    grad_eps = self.adv_eps * param.abs().detach()
                    self.backup_eps[name] = (
                        self.backup[name] - grad_eps,
                        self.backup[name] + grad_eps,
                    )

    def _restore(self,):
        for name, param in self.model.named_parameters():
            if name in self.backup:
                param.data = self.backup[name]
        self.backup = {}
        self.backup_eps = {}
