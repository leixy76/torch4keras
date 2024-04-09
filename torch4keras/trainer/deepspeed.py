from torch.utils.data import DataLoader
from torch4keras.snippets import JsonConfig, log_info, log_warn, log_warn_once
from torch4keras.snippets import argument_parse
from torch4keras.snippets import print_table, json_flat
from typing import Union
import os
import math
import inspect
from .base import Trainer


class DeepSpeedTrainer(Trainer):
    '''deepspeed来训练'''
    def __init__(self, module, verbose=0):
        super().__init__(module)
        self.model = module

        # 参数解析
        ds_args = DeepSpeedArgs(argument_parse())
        ds_args.set_default_args()  # 设置默认的一些参数
        ds_args.trainer_config_process(auto_find_batch_size=False)  # 设置一些auto的参数
        self.ds_config = ds_args.ds_config

        if verbose > 0:
            log_info('Deepspeed config listed below.')
            print_table(json_flat(self.ds_config), headers=['config_name', 'config_value'])
    
    def _prepare_inputs(self, train_dataloader:DataLoader, steps_per_epoch:Union[int,None], epochs:int, verbose:int):
        # batch_size需要使用deepspeed config中的train_batch_size/train_micro_batch_size_per_gpu
        if train_dataloader.batch_sampler is not None:
            btz = train_dataloader.batch_sampler.batch_size
            btz_ds = self.ds_config.train_batch_size
            btz_ds_per = self.ds_config.train_micro_batch_size_per_gpu
            if btz != btz_ds:
                log_warn_once(f'Use deepspeed config `train_batch_size`={btz_ds} and `train_micro_batch_size_per_gpu`={btz_ds_per} instead of `batch_size`={btz}')
            train_dataloader.batch_sampler.batch_size = self.ds_config.train_batch_size
        super()._prepare_inputs(train_dataloader, steps_per_epoch, epochs, verbose)


    def compile(self, *args, log_level='warning', inference=False, master_rank=0, **kwargs):
        super().compile(*args, **kwargs)
        import deepspeed
        from deepspeed.utils import logger as ds_logger
        import logging
        log_levels = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
            "critical": logging.CRITICAL,
        }

        ds_logger.setLevel(log_levels.get(log_level, logging.WARNING))

        if inference:
            # only Z3 makes sense for the inference
            log_warn("ZeRO inference only makes sense with ZeRO Stage 3")
            self.optimizer, self.scheduler = None, None
            model_parameters = None
        else:
            model_parameters = list(filter(lambda p: p.requires_grad, self.model.parameters()))
        
        kwargs = {
            "model": self.model,  # deepspeed的forward默认是计算到loss输出的
            "model_parameters": model_parameters,
            "config_params": self.ds_config,
            "optimizer": self.optimizer,
            "lr_scheduler": self.scheduler,
        }
        if self.ds_config.get('zero_optimization', {}).get('offload_optimizer', {}).get('device') == 'cpu':
            kwargs.pop('optimizer')
            if self.optimizer is not None:
                self.optimizer = None
                log_warn('You may not use custom optimizer when offload_optimizer=`cpu`')
        self.deepspeed_engine, self.optimizer, _, self.scheduler = deepspeed.initialize(**kwargs)
        self.verbose = 1 if self.deepspeed_engine.local_rank == master_rank else 0

    def unwrap_model(self):
        # 执行deepspeed_engine的forward
        return self.deepspeed_engine

    def loss_backward(self, loss):
        self.deepspeed_engine.backward(loss)
        return loss
    
    def step(self):
        self.deepspeed_engine.step()

    def resume_from_checkpoint(self, *args, **kwargs):
        from deepspeed import DeepSpeedEngine
        kwargs_ = {
            k: v for k, v in kwargs.items() if k in inspect.signature(DeepSpeedEngine.load_checkpoint).parameters
        }
        save_dir = args[0] if len(args) > 0 else kwargs['save_dir']
        return self.deepspeed_engine.load_checkpoint(save_dir, **kwargs_)

    def save_to_checkpoint(self, *args, **kwargs):
        from deepspeed import DeepSpeedEngine
        kwargs_ = {
            k: v for k, v in kwargs.items() if k in inspect.signature(DeepSpeedEngine.save_checkpoint).parameters
        }
        save_dir = args[0] if len(args) > 0 else kwargs['save_dir']
        return self.deepspeed_engine.save_checkpoint(save_dir, **kwargs_)


class DeepSpeedArgs():
    '''deepspeed的config设置, 含自动填充auto
    '''
    def __init__(self, arguements) -> None:
        self.train_args = arguements
        self.ds_config = JsonConfig(arguements.deepspeed)
        self.mismatches = []
        
    def set_default_args(self):
        '''设置默认的参数，用于deepspeed里面参数设置为auto的情况'''
        self.ds_config.steps_per_print = self.ds_config.get('steps_per_print', 1e9)  # 默认不打印, 防止进度条打印问题

        # 训练参数的默认参数
        self.train_args.world_size = int(os.environ["WORLD_SIZE"])
        self.train_args.per_device_train_batch_size = self.train_args.get('per_device_train_batch_size', 8)
        self.train_args.gradient_accumulation_steps = self.train_args.get('gradient_accumulation_steps', 1)
        self.train_args.max_grad_norm = self.train_args.get('max_grad_norm', 1.0)
        self.train_args.learning_rate = self.train_args.get('learning_rate', 5e-5)
        self.train_args.adam_beta1 = self.train_args.get('adam_beta1', 0.9)
        self.train_args.adam_beta2 = self.train_args.get('adam_beta2', 0.999)
        self.train_args.adam_epsilon = self.train_args.get('adam_epsilon', 1e-8)
        self.train_args.weight_decay = self.train_args.get('weight_decay', 0.0)
        self.train_args.fp16 = self.train_args.get('fp16', False)
        self.train_args.fp16_full_eval = self.train_args.get('fp16_full_eval', False)
        self.train_args.fp16_opt_level = self.train_args.get('fp16_opt_level', "O1")
        self.train_args.fp16_backend = self.train_args.get('fp16_backend', "auto")
        self.train_args.bf16 = self.train_args.get('bf16', False)
        self.train_args.bf16_full_eval = self.train_args.get('bf16_full_eval', False)
        self.train_args.warmup_steps = self.train_args.get('warmup_steps', 0)
        self.train_args.warmup_ratio = self.train_args.get('warmup_ratio', 0.0) 

    def find_config_node(self, ds_key_long):
        config = self.ds_config

        # find the config node of interest if it exists
        nodes = ds_key_long.split(".")
        ds_key = nodes.pop()
        for node in nodes:
            config = config.get(node)
            if config is None:
                return None, ds_key

        return config, ds_key
    
    def fill_match(self, ds_key_long, b4t_val, must_match=True):
        """
        A utility method that massages the config file and can optionally verify that the values match.

        1. Replace "auto" values with `TrainingArguments` value.

        2. If it wasn't "auto" and `must_match` is true, then check that DS config matches Trainer
        config values and if mismatched add the entry to `self.mismatched` - will assert during
        `trainer_config_finalize` for one or more mismatches.

        """
        config, ds_key = self.find_config_node(ds_key_long)
        if config is None:
            return

        if config.get(ds_key) == "auto":
            config[ds_key] = b4t_val
            return

        if not must_match:
            return

        ds_val = config.get(ds_key)
        if ds_val is not None and ds_val != b4t_val:
            self.mismatches.append({'ds_name': ds_key_long, 'ds_value': ds_val, 'b4t_value': b4t_val})

    def get_value(self, ds_key_long, default=None):
        """
        Returns the set value or `default` if no value is set
        """
        config, ds_key = self.find_config_node(ds_key_long)
        if config is None:
            return default
        return config.get(ds_key, default)
    
    def trainer_config_process(self, args=None, auto_find_batch_size=False):
        """自动填充和替换ds_config中的auto选项
        """
        if args is None:
            args = self.train_args

        # DeepSpeed does:
        # train_batch_size = world_size * train_micro_batch_size_per_gpu * gradient_accumulation_steps
        train_batch_size = args.world_size * args.per_device_train_batch_size * args.gradient_accumulation_steps
        self.fill_match("train_micro_batch_size_per_gpu", args.per_device_train_batch_size, must_match=not auto_find_batch_size)
        self.fill_match("gradient_accumulation_steps", args.gradient_accumulation_steps)
        self.fill_match("train_batch_size", train_batch_size, must_match = not auto_find_batch_size)
        self.fill_match("gradient_clipping", args.max_grad_norm)

        self.fill_match("optimizer.params.lr", args.learning_rate)
        self.fill_match("optimizer.params.betas", [args.adam_beta1, args.adam_beta2])
        self.fill_match("optimizer.params.eps", args.adam_epsilon)
        self.fill_match("optimizer.params.weight_decay", args.weight_decay)

        self.fill_match("scheduler.params.warmup_min_lr", 0, must_match=False)  # not a trainer arg
        self.fill_match("scheduler.params.warmup_max_lr", args.learning_rate)
        # total_num_steps - will get set in trainer_config_finalize

        # fp16
        if args.fp16 or args.fp16_full_eval:
            fp16_backend = "apex" if args.fp16_backend == "apex" else "amp"
        else:
            fp16_backend = None

        # amp: similar to the pytorch native amp - it has a bunch of optional params but we won't set
        # any here unless the user did the work
        self.fill_match("fp16.enabled", ((args.fp16 or args.fp16_full_eval) and fp16_backend == "amp"))

        # apex: delegates amp work to apex (which needs to be available), but it cannot be used with any
        # ZeRO features
        self.fill_match("amp.enabled", fp16_backend == "apex")
        self.fill_match("amp.opt_level", args.fp16_opt_level)

        self.fill_match("bf16.enabled", (args.bf16 or args.bf16_full_eval))

        ''' 以下逻辑为transformers中trainer_config_finalize修改'''
        # deal with config keys that use `auto` value and rely on model's hidden_size
        hidden_size_based_keys = [
            "zero_optimization.reduce_bucket_size",
            "zero_optimization.stage3_prefetch_bucket_size",
            "zero_optimization.stage3_param_persistence_threshold",
        ]
        hidden_size_auto_keys = [x for x in hidden_size_based_keys if self.get_value(x) == 'auto']

        if len(hidden_size_auto_keys) > 0:
            if hasattr(self.model.config, "hidden_size"):
                hidden_size = self.ds_config.hidden_size
            elif hasattr(self.ds_config, "hidden_sizes"):
                # if there are many hidden sizes pick the largest one
                hidden_size = max(self.ds_config.hidden_sizes)
            else:
                raise ValueError(
                    "The model's config file has neither `hidden_size` nor `hidden_sizes` entry, "
                    "therefore it's not possible to automatically fill out the following `auto` entries "
                    f"in the DeepSpeed config file: {hidden_size_auto_keys}. You can fix that by replacing "
                    "`auto` values for these keys with an integer value of your choice."
                )

            self.fill_match("zero_optimization.reduce_bucket_size", hidden_size * hidden_size, must_match=False)
            if self.get_value("zero_optimization.stage", -1) == 3:
                # automatically assign the optimal config values based on model config
                self.fill_match("zero_optimization.stage3_prefetch_bucket_size", 0.9 * hidden_size * hidden_size, must_match=False)
                self.fill_match("zero_optimization.stage3_param_persistence_threshold", 10 * hidden_size, must_match=False)

        # scheduler
        if hasattr(self, 'totel_steps'):
            self.fill_match("scheduler.params.total_num_steps", self.total_steps)
            self.fill_match("scheduler.params.warmup_num_steps", (self.ds_config.warmup_steps if self.ds_config.warmup_steps > 0 
                                                                else math.ceil(self.total_steps * self.ds_config.warmup_ratio)))

        if len(self.mismatches) > 0:
            log_warn('deepspeed config values not match bert4torch settings')
            print_table(self.mismatches)