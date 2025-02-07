import spacy
import argparse
import torch
import gloria
import datetime
import os
import numpy as np

from dateutil import tz
from omegaconf import OmegaConf
from pytorch_lightning import seed_everything
from pytorch_lightning import loggers as pl_loggers
from pytorch_lightning.trainer import Trainer
from pytorch_lightning.callbacks import (
    ModelCheckpoint,
    EarlyStopping,
    LearningRateMonitor,
)
from gloria.lightning.callbacks import EvaluateLocalization, WeightInstancesByLocalization
from gloria.datasets.mimic_for_gloria import GloriaCollateFn

torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = True


def get_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--config",
        metavar="base_config.yaml",
        help="paths to base config",
        required=True,
    )
    parser.add_argument(
        "--train", action="store_true", default=False, help="specify to train model"
    )
    parser.add_argument(
        "--val", action="store_true", default=False, help="specify to validate model"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        default=False,
        help="specify to test model"
        "By default run.py trains a model based on config file",
    )
    parser.add_argument(
        "--ckpt_path", type=str, default=None, help="Checkpoint path for the save model"
    )
    parser.add_argument("--random_seed", type=int, default=None, help="Random seed")
    parser.add_argument(
        "--train_pct", type=float, default=1.0, help="Percent of training data"
    )
    parser.add_argument(
        "--splits",
        type=int,
        default=1,
        help="Train on n number of splits used for training. Defaults to 1",
    )
    parser.add_argument(
        "--mask_mode",
        default=None
    )
    parser.add_argument(
        "--mask_prob",
        type=float,
        default=None
    )
    parser.add_argument(
        "--no_attn_vec", action="store_true", default=False, help="specify to validate model"
    )
    parser.add_argument(
        "--no_attn_loss_weight",
        type=float,
        default=None
    )
    parser.add_argument(
        "--attention_divergence_loss_weight",
        type=float,
        default=None
    )
    parser.add_argument(
        "--attention_entropy_loss_weight",
        type=float,
        default=None
    )
    parser.add_argument(
        "--global_loss_weight",
        type=float,
        default=None
    )
    parser.add_argument(
        "--resume", action="store_true", default=False, help="specify whether to resume training from checkpoint"
    )
    parser.add_argument(
        "--train_last_local_image_layer", action="store_true", default=False
    )
    parser.add_argument(
        "--train_prompt", action="store_true", default=False
    )
    parser.add_argument(
        "--randomize_objects_mode",
        type=str,
        default=None
    )
    parser.add_argument(
        "--swap_left_right", action="store_true", default=False
    )
    parser.add_argument(
        "--generate_sent", action="store_true", default=False
    )
    parser.add_argument(
        "--swap_conditions", action="store_true", default=False
    )
    parser = Trainer.add_argparse_args(parser)

    return parser


def main(cfg, args):

    # get datamodule
    dm = gloria.builder.build_data_module(cfg)

    # define lightning module
    if args.ckpt_path is not None:
        cfg.ckpt_path = args.ckpt_path
        if args.resume:
            print('resuming from checkpoint:', args.ckpt_path)
        else:
            print('loading from checkpoint:', args.ckpt_path)
    model = gloria.builder.build_lightning_model(
        cfg, dm, ckpt=args.ckpt_path if not args.resume else None)

    # logging
    if "logger" in cfg.lightning:
        logger_type = cfg.lightning.logger.pop("logger_type")
        logger_class = getattr(pl_loggers, logger_type)
        cfg.lightning.logger.name = f"{cfg.experiment_name}_{cfg.extension}"
        logger = logger_class(**cfg.lightning.logger)
        cfg.lightning.logger.logger_type = logger_type
    else:
        logger = None

    # callbacks
    callbacks = []
    if "logger" in cfg.lightning:
        callbacks.append(LearningRateMonitor(logging_interval="step"))
    if "checkpoint_callback" in cfg.lightning:
        checkpoint_callback = ModelCheckpoint(**cfg.lightning.checkpoint_callback)
        callbacks.append(checkpoint_callback)
    if "early_stopping_callback" in cfg.lightning:
        early_stopping_callback = EarlyStopping(**cfg.lightning.early_stopping_callback)
        callbacks.append(early_stopping_callback)
    if "logger" in cfg.lightning and cfg.train.scheduler is not None:
        lr_monitor = LearningRateMonitor(logging_interval="step")
        callbacks.append(lr_monitor)
    if "evaluate_localization" in cfg.lightning:
        gloria_collate_fn = GloriaCollateFn(cfg, 'test')
        evaluate_localization = EvaluateLocalization(
            gloria_collate_fn, save_dir=cfg.output_dir, **cfg.lightning.evaluate_localization)
#         example_batch = next(iter(dm.train_dataloader()))
#         evaluate_localization.get_windows(example_batch['imgs'][0, 0].shape, gloria=model)
        callbacks.append(evaluate_localization)
    else:
        evaluate_localization = None
    if "weight_instances_by_localization" in cfg.lightning:
        callbacks.append(WeightInstancesByLocalization(dm.dm, **cfg.lightning.weight_instances_by_localization))

    # setup pytorch-lightning trainer
    cfg.lightning.trainer.val_check_interval = args.val_check_interval
    cfg.lightning.trainer.auto_lr_find = args.auto_lr_find
    trainer_args = argparse.Namespace(**cfg.lightning.trainer)
    trainer = Trainer.from_argparse_args(
        args=trainer_args, deterministic=True, callbacks=callbacks, logger=logger
    )

    # learning rate finder
    if trainer_args.auto_lr_find is not False:
        lr_finder = trainer.tuner.lr_find(model, datamodule=dm)
        new_lr = lr_finder.suggestion()
        model.lr = new_lr
        print("=" * 80 + f"\nLearning rate updated to {new_lr}\n" + "=" * 80)

    #if cfg.model.train_last_local_image_layer or cfg.model.train_prompt:
    #    for p in model.parameters():
    #        p.requires_grad = False
    #    if cfg.model.train_last_local_image_layer:
    #        for p in model.gloria.img_encoder.model.layer3.parameters():
    #            p.requires_grad = True
    #    if cfg.model.train_prompt:
    #        for p in model.gloria.text_encoder.model.embeddings.parameters():
    #            p.requires_grad = True

    if args.train:
        trainer.fit(model, dm)
    if args.val or args.test:
        if evaluate_localization is not None:
            evaluate_localization.val_save_full_data = True
        if args.train:
            print('loading from best checkpoint:', checkpoint_callback.best_model_path)
            model = model.__class__.load_from_checkpoint(checkpoint_callback.best_model_path, cfg=cfg)
        if args.val:
            trainer.validate(model=model, datamodule=dm)
        if args.test:
            trainer.test(model=model, datamodule=dm)

    # save top weights paths to yaml
    if "checkpoint_callback" in cfg.lightning:
        ckpt_paths = os.path.join(
            cfg.lightning.checkpoint_callback.dirpath, "best_ckpts.yaml"
        )
        checkpoint_callback.to_yaml(filepath=ckpt_paths)


if __name__ == "__main__":
    # parse arguments
    parser = get_parser()
    args = parser.parse_args()
    cfg = OmegaConf.load(args.config)
    if args.random_seed is not None:
        cfg.random_seed = args.random_seed
    if args.mask_mode is not None:
        cfg.data.mask_mode = args.mask_mode
    if args.mask_prob is not None:
        cfg.data.mask_prob = args.mask_prob
    if args.ckpt_path is not None and args.resume:
        cfg.lightning.trainer.resume_from_checkpoint = args.ckpt_path
    cfg.model.gloria.no_attn_vec = args.no_attn_vec
    if args.no_attn_loss_weight is not None:
        cfg.model.gloria.no_attn_loss_weight = args.no_attn_loss_weight
    if args.attention_divergence_loss_weight is not None:
        cfg.model.gloria.attention_divergence_loss_weight = args.attention_divergence_loss_weight
    if args.attention_entropy_loss_weight is not None:
        cfg.model.gloria.attention_entropy_loss_weight = args.attention_entropy_loss_weight
    if args.global_loss_weight is not None:
        cfg.model.gloria.global_loss_weight = args.global_loss_weight
    if args.gradient_clip_val != 0:
        cfg.lightning.trainer.gradient_clip_val = args.gradient_clip_val
    cfg.model.gloria.train_last_local_image_layer = args.train_last_local_image_layer
    cfg.model.gloria.train_prompt = args.train_prompt

    if args.randomize_objects_mode is not None:
        cfg.data.randomize_objects_mode = args.randomize_objects_mode
    cfg.data.swap_left_right = args.swap_left_right
    cfg.data.generate_sent = args.generate_sent
    cfg.data.swap_conditions = args.swap_conditions

    # edit experiment name
    cfg.data.frac = args.train_pct
    if cfg.trial_name is not None:
        cfg.experiment_name = f"{cfg.experiment_name}_{cfg.trial_name}"
    if args.splits is not None:
        cfg.experiment_name = f"{cfg.experiment_name}_{args.train_pct}"  # indicate % data used in trial name

    # loop over the number of independent training splits, defaults to 1 split
    for split in np.arange(args.splits):

        # get current time
        now = datetime.datetime.now(tz.tzlocal())
        timestamp = now.strftime("%Y_%m_%d_%H_%M_%S")

        # random seed
        args.random_seed = split + cfg.random_seed
        seed_everything(args.random_seed)

        # set directory names
        cfg.extension = str(args.random_seed) if args.splits != 1 else timestamp
        cfg.output_dir = f"{cfg.base_output_dir}/{cfg.experiment_name}/{cfg.extension}"
        print('output_dir:', cfg.output_dir)
        cfg.lightning.checkpoint_callback.dirpath = os.path.join(
            cfg.lightning.checkpoint_callback.dirpath,
            f"{cfg.experiment_name}/{cfg.extension}",
        )

        # create directories

        if "logger" in cfg.lightning:
            if not os.path.exists(cfg.lightning.logger.save_dir):
                os.makedirs(cfg.lightning.logger.save_dir)
        if not os.path.exists(cfg.lightning.checkpoint_callback.dirpath):
            os.makedirs(cfg.lightning.checkpoint_callback.dirpath)
        if not os.path.exists(cfg.output_dir):
            os.makedirs(cfg.output_dir)

        # save config
        config_path = os.path.join(cfg.output_dir, "config.yaml")
        with open(config_path, "w") as fp:
            OmegaConf.save(config=cfg, f=fp.name)

        main(cfg, args)
