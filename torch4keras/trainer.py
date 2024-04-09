from torch import nn
import torch
from torch.optim import Optimizer
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.data import DataLoader
from torch4keras.snippets import DottableDict, JsonConfig, metric_mapping, get_parameter_device, log_info, log_warn, log_warn_once, seed_everything
from torch4keras.snippets import print_trainable_parameters, colorful, send_email, load_checkpoint, save_checkpoint, argument_parse
from torch4keras.snippets import print_table, json_flat
from torch4keras.callbacks import KerasProgbar, SmoothMetricsCallback, TqdmProgbar, ProgressBar2Progbar, Callback, CallbackList, History
from collections import OrderedDict
from typing import Union, List, Literal, Tuple, Set, Callable, Optional
from inspect import isfunction
import os
import sys
import math
import re
import traceback
import inspect


class Trainer:
    '''Trainer, 传入Module实例

    :param module: None/nn.Module, nn.Module()的模型实例
    '''
    def __init__(self, module:nn.Module=None):
        super(Trainer, self).__init__()
        self.initialize(module)
    
    def initialize(self, module:nn.Module=None):
        # 传入Module实例方式
        if module is not None:
            assert isinstance(module, nn.Module), 'Args `module` only support nn.Module format'
            self.module = module

        self.global_step, self.local_step, self.total_steps, self.batch_step = 0, 0, 0, 0
        self.epoch, self.steps_per_epoch, self.train_dataloader = 0, None, None
        self.resume_step, self.resume_epoch = 0, 0
        self.retain_graph = False  # loss.backward()是否保留计算图
        self.move_to_model_device = True  # 自动把tensor转到model所在的device
        self.log_first_step = False  # 是否打印第一个step的数据
        self.criterion = None  # criterion
        self.optimizer = None  # optimizer
        self.scheduler = None  # scheduler
        self.callbacks = []  # 所有的Callbacks, 如果fit中不传入, 则默认为[progbarlogger, smoothmetrics, history]三项
        self.run_callbacks = True  # 是否运行Callbacks, 目前主要是在DDP模式下运用
        self.loss2metrics = True  # 把loss_detail打印在进度条的metrics上
        # add_module(self)  # 增加nn.Module的成员方法

    def compile(self, loss:Optional[Union[Callable, nn.Module]]=None, optimizer:Optimizer=None, scheduler:LambdaLR=None, clip_grad_norm:float=None, 
                mixed_precision:Literal[True, False, 'fp16', 'bf16']=False, metrics:Union[str, dict, Callable, List[Union[str, dict, Callable]]]=None, 
                grad_accumulation_steps:int=1, progbar_type:Literal['keras', 'tqdm', 'progressbar2']='keras', progbar_width:int=None,
                stateful_metrics:Union[str, Set[str], Tuple[str], List[str]]=None, smooth_interval:int=100, **kwargs):
        '''complile: 定义loss, optimizer, metrics等参数
        
        :param loss: loss
        :param optimizer: 优化器
        :param scheduler: lr_scheduler
        :param clip_grad_norm: float, 是否使用梯度裁剪, 默认为False
        :param mixed_precision: bool, 是否使用混合精度, 默认为False
        :param metrics: str/List[str]/dict, 训练过程中需要打印的指标, loss相关指标默认会打印, 目前支持accuracy, 也支持自定义metric, 形式为{key: func}
        :param grad_accumulation_steps: int, 梯度累积步数, 默认为1
        :param bar: str, 使用进度条的种类, 从kwargs中解析, 默认为keras, 可选keras, tqdm, progressbar2

        > 进度条的配置
            progbar_type: str, 默认为keras, 可选keras, tqdm, progressbar2
            width: int, keras进度条下表示进度条的长度
        > 指标平滑的配置, 默认为None表示采取默认平滑设置; 传入False表示不使用平滑
            stateful_metrics: List[str], 表示不使用指标平滑仅进行状态记录的metric, 指标抖动会更加明显, 默认为None表示使用指标平滑
            smooth_interval: int, 表示指标平滑时候的累计步数, 默认为100

        :return: None
        '''
        self.criterion = loss or self.criterion  # loss
        self.optimizer = optimizer or self.optimizer  # 优化器
        self.scheduler = scheduler or self.scheduler  # lr_scheduler
        self.clip_grad_norm = clip_grad_norm  # 梯度裁剪
        self.grad_accumulation_steps = grad_accumulation_steps  # 梯度累积

        # 混合精度
        assert mixed_precision in {True, False, 'fp16', 'bf16'}
        self.mixed_precision_mode = 'fp16' if mixed_precision is True else mixed_precision
        if self.mixed_precision_mode:
            self.autocast = torch.cuda.amp.autocast
            self.scaler = torch.cuda.amp.GradScaler()

        # 训练过程观测的指标
        self.metrics = OrderedDict({'loss': None})
        if metrics is None:
            metrics = []
        elif isinstance(metrics, (str, dict)) or isfunction(metrics):
            metrics = [metrics]
        for metric in metrics:
            # 字符类型, 目前仅支持accuracy
            if isinstance(metric, str) and metric != 'loss':
                self.metrics[metric] = None
            # 字典形式 {metric: func}
            elif isinstance(metric, dict):
                self.metrics.update(metric)
            # 函数形式, key和value都赋值metric
            elif isfunction(metric):
                self.metrics.update({metric: metric})
            else:
                raise TypeError('Args metrics only support `String, Dict, Callable, List[String, Dict, Callable]` format')

        # 进度条参数
        assert progbar_type in {'keras', 'tqdm', 'progressbar2'}
        self.progbar_config = {'bar': progbar_type, 'width': progbar_width}
        self.progbar_config = {k:v for k,v in self.progbar_config.items() if v is not None}

        # smooth_metrics参数: 默认平滑
        self.smooth_metrics_config = {'stateful_metrics': stateful_metrics, 'interval': smooth_interval, 'verbose': kwargs.get('verbose')}
        self.smooth_metrics_config = {k:v for k,v in self.smooth_metrics_config.items() if v is not None}

        # 其他参数设置
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def print_trainable_parameters(self):
        '''打印可训练的参数量'''
        print_trainable_parameters(self.unwrap_model())

    @property
    def device(self) -> torch.device:
        '''获取model所在的device'''
        if hasattr(self, '_device'):
            return self._device
        return get_parameter_device(self.unwrap_model())

    @device.setter
    def device(self, value):
        '''允许修改self.device'''
        self._device = value

    def _move_to_model_device(self, inputs:Union[torch.Tensor, tuple, list, dict]):
        '''遍历并转移到model.device上（递归）'''
        if self.move_to_model_device:
            if isinstance(inputs, torch.Tensor) and hasattr(self, 'device') and (inputs.device != self.device):
                inputs = inputs.to(self.device)
            elif isinstance(inputs, (tuple, list)):
                inputs = list(inputs) if isinstance(inputs, tuple) else inputs
                for i, ts in enumerate(inputs):
                    inputs[i] = self._move_to_model_device(ts)
            elif isinstance(inputs, dict):
                for k, v in inputs.items():
                    inputs[k] = self._move_to_model_device(v)
        return inputs

    def _log_first_step(self, resume_step, train_X, train_y):
        '''打印第一个step的数据'''
        if self.log_first_step and self.verbose and (self.global_step == resume_step):
            print(colorful('[Train_data]: ', color='green'), + train_X)
            print(colorful('[Label]: ', color='green'), + train_y)

    def _forward(self, *inputs, **input_kwargs):
        '''调用模型的forward, 方便下游继承的时候可以自定义使用哪个模型的forward
        '''
        return self._argparse_forward(self.unwrap_model(), *inputs, **input_kwargs)

    def _argparse_forward(self, model, *inputs, **input_kwargs):
        '''调用模型的forward
        如果传入了网络结构module, 则调用module的forward; 如果是继承方式, 则调用自身的forward
        这里声明为staticmethod的话, 使用add_trainer会有问题
        '''
        if (len(inputs)==1) and isinstance(inputs[0], (tuple,list)):  # 防止([])嵌套
            inputs = inputs[0]
        
        if isinstance(inputs, torch.Tensor):  # tensor不展开
            return model.forward(inputs, **input_kwargs)
        elif isinstance(inputs, (tuple, list)):
            return model.forward(*inputs, **input_kwargs)
        else:
            return model.forward(inputs, **input_kwargs)

    def train_step(self, train_X, train_y):
        ''' Perform a training step on a batch of inputs. '''
        # 计算loss
        if self.mixed_precision_mode:
            with self.autocast(dtype=torch.float16 if self.mixed_precision_mode=='fp16' else torch.bfloat16):
                output = self._forward(train_X)
                loss_detail = self.criterion(output, train_y)
        else:
            output = self._forward(train_X)
            loss_detail = self.criterion(output, train_y)

        # 整理loss
        if isinstance(loss_detail, torch.Tensor):
            loss = loss_detail
            loss_detail = {}
        elif isinstance(loss_detail, dict):
            loss = loss_detail['loss']  # 还存在其他loss, 仅用于打印
            del loss_detail['loss']
        elif isinstance(loss_detail, (tuple, list)):
            loss = loss_detail[0]
            loss_detail = {f'loss{i}':v for i, v in enumerate(loss_detail[1:], start=1)}
        else:
            raise ValueError('Return loss only support `Tensor/dict/tuple/list` format')

        # 梯度累积
        loss = loss / self.grad_accumulation_steps if self.grad_accumulation_steps > 1 else loss

        # loss backward
        loss = self.loss_backward(loss)
        loss_detail = {k: (v.item() if isinstance(v, torch.Tensor) else v) / self.grad_accumulation_steps for k, v in loss_detail.items()}
        return output, loss, loss_detail

    def loss_backward(self, loss):
        '''loss.backward'''
        self.scale_before_step = 0
        if self.mixed_precision_mode:  # 混合精度
            self.scale_before_step = self.scaler.get_scale()
            self.scaler.scale(loss).backward(retain_graph=self.retain_graph)
        else:
            loss.backward(retain_graph=self.retain_graph)
        return loss
    
    def step(self):
        '''参数更新'''
        skip_scheduler = False
        # 混合精度
        if self.mixed_precision_mode:
            self.scaler.unscale_(self.optimizer)
            if self.clip_grad_norm is not None:  # 梯度裁剪
                torch.nn.utils.clip_grad_norm_(self.unwrap_model().parameters(), self.clip_grad_norm)
            self.scaler.step(self.optimizer)
            self.scaler.update()
            skip_scheduler = self.scaler.get_scale() != self.scale_before_step
        else:
            if self.clip_grad_norm is not None:  # 梯度裁剪
                torch.nn.utils.clip_grad_norm_(self.unwrap_model().parameters(), self.clip_grad_norm)
            self.optimizer.step()

        self.optimizer.zero_grad()  # 清梯度
        if (self.scheduler is not None) and not skip_scheduler:
            if isinstance(self.scheduler, (tuple, list)):
                for scheduler in self.scheduler:
                    scheduler.step()
            else:
                self.scheduler.step()

    def _prepare_inputs(self, train_dataloader:DataLoader, steps_per_epoch:Union[int,None], epochs:int, verbose:int):
        '''对fit的输入进行类型检查并置为成员变量'''
        if not hasattr(train_dataloader, '__len__'):
            assert steps_per_epoch is not None, 'Either train_dataloader has attr `__len__` or steps_per_epoch is not None'
        if steps_per_epoch is None:
            self.steps_per_epoch = math.ceil(len(train_dataloader) // self.grad_accumulation_steps)
        else:
            self.steps_per_epoch = steps_per_epoch
        self.batch_size = train_dataloader.batch_size
        self.epochs = epochs
        self.total_steps = self.steps_per_epoch * epochs
        self.train_dataloader = train_dataloader  # 设置为成员变量, 可由外部的callbacks进行修改
        self.train_dataloader_iter = iter(self.train_dataloader)  # 循环epoch时不重生成
        self.verbose = self.verbose if hasattr(self, 'verbose') else verbose

    def _prepare_callbacks(self, callbacks:Union[Callback, List[Callback]]=None):
        '''callbacks设置'''
        if callbacks is None:
            callbacks = []
        elif isinstance(callbacks, Callback):
            callbacks = [callbacks]
        for callback in callbacks:
            assert isinstance(callback, Callback), f"Args `callbacks` only support Callback(), but got {type(callback)}"

        history = History()
        callbacks_ = []

        # 指标平滑
        if any([isinstance(i, SmoothMetricsCallback) for i in callbacks]):
            # 用户自定的callbacks中包含了SmoothMetricsCallback
            log_warn(f'SmoothMetricsCallback already in use and args `smooth_metrics_config` will be ignored')
            smooth_callback = [callback for callback in callbacks if isinstance(callback, SmoothMetricsCallback)][0]
            callbacks_.append(smooth_callback)
            callbacks = [callback for callback in callbacks if not isinstance(callback, SmoothMetricsCallback)]
        elif self.smooth_metrics_config.get('interval') is not None:
            smooth_callback = SmoothMetricsCallback(**self.smooth_metrics_config)
            callbacks_.append(smooth_callback)
        else:
            # 不平滑
            smooth_callback = None

        # 检查指标平滑的设置和后续callback的设置的interval是不是一致
        for callback in callbacks:
            if hasattr(callback, 'interval') and (smooth_callback is not None) and (callback != smooth_callback) and \
                (callback.interval is not None) and (callback.interval % smooth_callback.interval != 0):
                log_warn(f'{type(callback).__name__}.interval={callback.interval} while SmoothMetricsCallback.interval={smooth_callback.interval}')
        
        # 进度条
        progbarlogger = None
        if self.verbose:
            if self.progbar_config['bar'] == 'keras':
                progbarlogger = KerasProgbar(**self.progbar_config)
            elif self.progbar_config['bar'] == 'tqdm':
                progbarlogger = TqdmProgbar(**self.progbar_config)
            elif self.progbar_config['bar'] == 'progressbar2':
                progbarlogger = ProgressBar2Progbar(**self.progbar_config)
            else:
                progbarlogger = KerasProgbar(**self.progbar_config)
            callbacks_.append(progbarlogger)

        callbacks_  += callbacks + [history]
        self.callbacks = CallbackList(callbacks_, run_callbacks=self.run_callbacks)
        callback_trainer = self
        callback_model = self.unwrap_model()
        params = {
            'epochs': self.epochs,
            'steps': self.steps_per_epoch,
            'verbose': self.verbose,
            'metrics': [i for i in self.metrics.keys() if isinstance(i, str)],
        }
        self.callbacks.set_all(trainer=callback_trainer, model=callback_model, optimizer=self.optimizer, scheduler=self.scheduler, params=params)
        callback_trainer.stop_training = False  # 在EarlyStopping中会重新设置
        return history, callback_trainer, progbarlogger

    def _prepare_nextbatch(self):
        '''准备下一个batch数据'''
        # 循环dataloader, 不要试用itertools的cycle, 遇到过变量不释放的问题
        try:
            batch = next(self.train_dataloader_iter)
            self.batch_step += 1
        except StopIteration:
            self.callbacks.on_dataloader_end()  # 适用于数据量较大时, 动态读取文件并重新生成self.train_dataloader的情况, 如预训练
            # DDP训练时候为了避免每个epoch样本一致, 修改随机种子
            if isinstance(self.train_dataloader.sampler, torch.utils.data.distributed.DistributedSampler) and \
                hasattr(self.train_dataloader.sampler, 'set_epoch'):
                self.train_dataloader.sampler.set_epoch(self.epoch)
            self.train_dataloader_iter = iter(self.train_dataloader)  # shuffle=True时候, 其实顺序也重新生成了
            self.batch_step = 0
            batch = next(self.train_dataloader_iter)

        batch = self._move_to_model_device(batch)
        return batch

    def fit(self, train_dataloader:DataLoader, steps_per_epoch:int=None, epochs:int=1, 
            callbacks:Union[Callback, List[Callback]]=None, verbose:int=1, **kwargs):
        ''' 模型训练
        :param train_dataloader: Dataloader, 训练数据集
        :param steps_per_epoch: int, 每个epoch训练的steps, 默认为None表示自行计算 
        :param epochs: int, 训练的轮次, 默认为1
        :param callbacks: Callback/List[Callback], 回调函数, 可调用预制的Callback或者自定义, 默认为None 
        :param verbose: int, 是否打印, 默认为1表示打印
        
        > 其他参数
        :param mail_receivers_when_error: str, 训练异常时候邮件通知
        :param save_ckpt_dir_when_error: str, 训练异常时候保存权重的路径
        :param save_batch_path_when_error: bool, 训练异常时候保存当前batch, 方便debug

        :return: History
        '''
        try:
            return self._fit(train_dataloader, steps_per_epoch, epochs, callbacks, verbose, **kwargs)
        except Exception as e:
            # 训练异常则发邮件
            error_msg = traceback.format_exc()
            mail_receivers_ = kwargs.get('mail_receivers_when_error')
            if mail_receivers_ is not None:
                mail_subject_ = kwargs.get('mail_subject_when_error') or "[ERROR] fit"
                mail_host_ = kwargs.get('mail_host_when_error')
                mail_user_ = kwargs.get('mail_user_when_error')
                mail_pwd_ = kwargs.get('mail_pwd_when_error')
                mail_sender_ = kwargs.get('mail_sender_when_error')
                send_email(mail_receivers_, mail_subject_, error_msg, mail_host=mail_host_, 
                           mail_user=mail_user_, mail_pwd=mail_pwd_, mail_sender=mail_sender_)

            # 训练异常则保存权重
            if (save_ckpt_dir_when_error := kwargs.get('save_ckpt_dir_when_error')) is not None:
                self.save_to_checkpoint(save_ckpt_dir_when_error, verbose=verbose, **kwargs)

            # 训练异常则打印当前batch
            if (save_batch_path_when_error := kwargs.get('save_batch_path_when_error')) is not None:
                os.makedirs(os.path.dirname(save_batch_path_when_error), exist_ok=True)
                torch.save({'train_X': self.train_X.cpu(), 'train_y': self.train_y.cpu()}, save_batch_path_when_error)
            
            raise e

    def _fit(self, train_dataloader:DataLoader, steps_per_epoch:int=None, epochs:int=1, 
             callbacks:Union[Callback, List[Callback]]=None, verbose:int=1, **kwargs):
        '''模型训练'''
        # 输入处理
        self._prepare_inputs(train_dataloader, steps_per_epoch, epochs, verbose)

        # 准备callbacks
        history, callback_trainer, progbarlogger  = self._prepare_callbacks(callbacks)

        #       epoch: 当前epoch
        # global_step: 当前全局训练步数
        #  local_step: 当前epoch内的训练步数, 不同epoch中相同local_step对应的batch数据不一定相同, 在steps_per_epoch=None时相同
        #  batch_step: 在dataloader中的index, 不同epoch中相同的bti对应的batch数据一般相同, 除非重新生成dataloader
        self.callbacks.on_train_begin()
        logs = self._log_init()  # 防止数据集为空时候
        for epoch in range(self.resume_epoch, epochs):
            self.epoch = epoch
            # resume_step：判断local_step的起点, 以及进度条的起始位置
            resume_step = self.resume_step if epoch==self.resume_epoch else 0
            self.callbacks.on_epoch_begin(self.global_step, self.epoch)
            if self.verbose:
                progbarlogger.seen = resume_step  # 这里设置进度条的seen, 在callbacks中也会修改
            
            for local_step in range(resume_step, self.steps_per_epoch):
                self.local_step = local_step
                self.global_step = self.epoch * self.steps_per_epoch + self.local_step
                logs = self._log_init()
                self.callbacks.on_batch_begin(self.global_step, self.local_step, logs)

                # forward和backward
                if not self.unwrap_model().training:
                    self.unwrap_model().train()  # 设置为train模式
                tr_loss, tr_loss_detail = 0, {}
                for _ in range(self.grad_accumulation_steps):
                    self.train_X, self.train_y = self._prepare_nextbatch()  # 获取下一个batch的训练数据
                    self._log_first_step(resume_step, self.train_X, self.train_y)  # log第一个step
                    output, loss, loss_detail = self.train_step(self.train_X, self.train_y)
                    self.callbacks.on_train_step_end()
                    tr_loss += loss.item()
                    for k, v in loss_detail.items():
                        tr_loss_detail[k] = tr_loss_detail.get(k, 0) + v
                # TODO: 理论上梯度累积时需对output和train_y进行合并, 主要是为了metric_mapping计算的准确
                
                # 参数更新
                self.step()

                # 添加loss至log打印
                logs.update(dict({'loss': tr_loss}, **tr_loss_detail))
                if self.verbose and self.loss2metrics and (self.global_step == resume_step):
                    # 把loss_detail添加到进度条metrics中
                    progbarlogger.add_metrics(list(tr_loss_detail.keys()), add_position=1)
                    
                # 添加metrics至log打印
                for metric, func in self.metrics.items():
                    perf = metric_mapping(metric, func, output, self.train_y)  # 内置的一些accuracy指标
                    if perf is not None:
                        if isfunction(metric):  # 直接传入回调函数(无key)
                            if self.verbose and (self.global_step == resume_step):
                                progbarlogger.add_metrics(list(perf.keys()))
                            logs.update(perf)
                        elif isinstance(metric, str):  # 直接传入回调函数(有key)
                            logs[metric] = perf

                self.callbacks.on_batch_end(self.global_step, self.local_step, logs)

            self.callbacks.on_epoch_end(self.global_step, self.epoch, logs)
            # TerminateOnNaN、EarlyStopping等停止训练策略
            if callback_trainer.stop_training:
                break
        self.callbacks.on_train_end(logs)
        return history

    def _log_init(self):
        '''获取batch_size, 主要是用于callback中的BaseLogger和Callback
        '''
        logs = {}

        # 添加lr
        try:
            logs['lr'] = self.optimizer.param_groups[0]["lr"]
        except:
            pass
        return logs

    @torch.no_grad()
    def predict(self, *inputs, **input_kwargs):
        '''模型预测, 调用forward()'''
        self.unwrap_model().eval()
        inputs = self._move_to_model_device(inputs)
        input_kwargs = self._move_to_model_device(input_kwargs)
        return self._forward(*inputs, **input_kwargs)
        
    def load_steps_params(self, save_path:str):
        '''导入训练过程参数
        
        :param save_path: str, 训练过程参数保存路径
        '''
        step_params = torch.load(save_path)
        self.resume_step = step_params['resume_step'] 
        self.resume_epoch = step_params['resume_epoch']
        return step_params

    def save_steps_params(self, save_path:str):
        '''保存训练过程参数

        :param save_path: str, 训练过程参数保存路径
        '''
        step_params = {'resume_step': (self.local_step+1) % self.steps_per_epoch, 
                       'resume_epoch': self.epoch + (self.local_step+1) // self.steps_per_epoch}
        save_dir = os.path.dirname(save_path)
        os.makedirs(save_dir, exist_ok=True)
        torch.save(step_params, save_path)

    def load_weights(self, load_path:Union[str,tuple,list], strict:bool=True, mapping:Union[dict,Callable]=None):
        '''加载模型权重, 支持加载权重文件list

        :param save_path: str/tuple/list, 权重加载路径
        :param strict: bool, torch.load()是否严格加载
        :param mapping: dict/func, 指定key的映射
            1. mapping=None, 表示按照模型自身结构加载, 一般加载finetune后使用save_weights()保存出来的权重
            2. mapping自定义, 根据用户自定义mapping来加载权重
        '''
        if isinstance(load_path, (tuple, list)):
            strict = False  # 加载多个权重文件时候, strict设置为False
        elif isinstance(load_path, str):
            load_path = [load_path]
        else:
            raise ValueError('Args `load_path` only support str/tuple/list format')
        
        mapping = mapping or dict()
        for load_path_i in load_path:
            state_dict = load_checkpoint(load_path_i)
            for k in list(state_dict.keys()):
                if isinstance(mapping, dict) and k in mapping:
                    state_dict[mapping[k]] = state_dict.pop(k)
                elif isinstance(mapping, Callable):
                    state_dict[mapping(k)] = state_dict.pop(k)
            self.unwrap_model().load_state_dict(state_dict, strict=strict)

    def save_weights(self, save_path:str, mapping:Union[dict,Callable]=None, trainable_only:bool=False):
        '''保存模型权重

        :param save_path: str, 权重保存路径
        :param mapping: dict/func, 指定key的映射
            1. mapping=None, 表示按照模型自身结构的key来保存, 后续可直接load_weights()加载
            2. mapping自定义, 根据用户自定义mapping来保存权重
        :param trainable_only: bool, 指定仅保存可训练参数
        '''
        state_dict = self.unwrap_model().state_dict()
        trainable_parameters = set(p for p,v in self.unwrap_model().named_parameters() if v.requires_grad)
        
        mapping = mapping or dict()
        for k in list(state_dict.keys()):
            # 只保存可训练的模型部分
            if trainable_only and (k not in trainable_parameters):
                continue
            if isinstance(mapping, dict) and k in mapping:
                state_dict[mapping[k]] = state_dict.pop(k)
            elif isinstance(mapping, Callable):
                state_dict[mapping(k)] = state_dict.pop(k)        
        save_checkpoint(state_dict, save_path)
        if trainable_only:
            params_all = sum(p.numel() for p in self.unwrap_model().parameters())
            params_trainable = sum(p.numel() for p in self.unwrap_model().parameters() if p.requires_grad)
            ratio = params_trainable/params_all*100
            log_info(f"Only trainable parameters saved and occupy {params_trainable}/{params_all}={ratio:.2f}%")

    def save_pretrained(self, save_path:str, weight_map:dict=None, mapping:Union[dict,Callable]=None):
        '''按照预训练模型的key来保存模型, 可供transformers包加载

        :param save_path: str, 保存的文件/文件夹路径
        '''
        state_dict = dict()
        for name, child in self.unwrap_model().named_children():
            if (name != '') and hasattr(child, 'save_pretrained'):
                tmp = child.save_pretrained(save_path, weight_map, mapping, write_to_disk=False)
                state_dict.update(tmp if tmp else {})
            else:
                state_dict.update({f'{name}.{k}': v for k,v in child.state_dict().items()})
        if len(state_dict) > 0:
            save_dir = None if re.search('\.[a-zA-z0-9]+$', save_path) else save_path
            save_checkpoint(state_dict, os.path.join(save_dir, 'pytorch_model.bin') if save_dir else save_path)
    
    def resume_from_checkpoint(self, save_dir:str=None, model_path:str=None, optimizer_path:str=None, scheduler_path:str=None, 
                               steps_params_path:str=None, mapping:Union[dict,Callable]=None, verbose:int=0, strict:bool=True, 
                               device=None, **kwargs):
        '''同时加载模型、优化器、训练过程参数

        :param save_dir: str, 保存目录
        :param model_path: str, 模型文件路径
        :param optimizer_path: str, 优化器文件路径
        :param scheduler_path: str, scheduler文件路径
        :param steps_params_path: str, 训练过程参数保存路径
        :param mapping: dict, 模型文件的mapping
        '''
        # 加载模型权重
        if model_path or save_dir:
            model_path = model_path or os.path.join(save_dir, 'model.pt')
            self.load_weights(model_path, strict=strict, mapping=mapping)
            if verbose == 1:
                log_info(f'Model weights successfuly resumed from {model_path}')

        # 加载优化器
        if optimizer_path or save_dir:
            optimizer_path = optimizer_path or os.path.join(save_dir, 'optimizer.pt')
            state_dict = torch.load(optimizer_path, map_location = device or self.device)
            self.optimizer.load_state_dict(state_dict)
            if verbose == 1:
                log_info(f'Optimizer successfuly resumed from {optimizer_path}')

        # 加载scheduler
        if (scheduler_path or save_dir) and (self.scheduler is not None):
            scheduler_path = scheduler_path or os.path.join(save_dir, 'scheduler.pt')
            state_dict = torch.load(scheduler_path, map_location = device or self.device)
            self.scheduler.load_state_dict(state_dict)
            if verbose == 1:
                log_info(f'Scheduler successfuly resumed from {scheduler_path}')

        # 加载训练进度参数
        if steps_params_path or save_dir:
            steps_params_path = steps_params_path or os.path.join(save_dir, 'steps_params.pt')
            self.load_steps_params(steps_params_path)
            if verbose == 1:
                log_info(f'Steps_params successfuly resumed from {steps_params_path}')

    def save_to_checkpoint(self, save_dir:str=None, model_path:str=None, optimizer_path:str=None, scheduler_path:str=None, 
                           steps_params_path:str=None, mapping:Union[dict,Callable]=None, trainable_only:bool=False, 
                           verbose:int=0, **kwargs):
        '''同时保存模型、优化器、训练过程参数、scheduler

        :param save_dir: str, 保存目录
        :param model_path: str, 模型文件路径
        :param optimizer_path: str, 优化器文件路径
        :param scheduler_path: str, scheduler文件路径
        :param steps_params_path: str, 训练过程参数保存路径
        :param mapping: dict/func, 模型文件的mapping
        :param trainable_only
        '''
        if model_path or save_dir:
            model_path = model_path or os.path.join(save_dir, 'model.pt')
            self.save_weights(model_path, mapping=mapping, trainable_only=trainable_only)
            if verbose == 1:
                log_info(f'Model weights successfuly saved to {model_path}')

        if optimizer_path or save_dir:
            optimizer_path = optimizer_path or os.path.join(save_dir, 'optimizer.pt')
            os.makedirs(os.path.dirname(optimizer_path), exist_ok=True)
            torch.save(self.optimizer.state_dict(), optimizer_path)
            if verbose == 1:
                log_info(f'Optimizer successfuly saved to {optimizer_path}')

        if (scheduler_path or save_dir) and (self.scheduler is not None):
            scheduler_path = scheduler_path or os.path.join(save_dir, 'scheduler.pt')
            os.makedirs(os.path.dirname(scheduler_path), exist_ok=True)
            torch.save(self.scheduler.state_dict(), scheduler_path)
            if verbose == 1:
                log_info(f'Scheduler successfuly saved to {scheduler_path}')

        if steps_params_path or save_dir:
            steps_params_path = steps_params_path or os.path.join(save_dir, 'steps_params.pt')
            self.save_steps_params(steps_params_path)
            if verbose == 1:
                log_info(f'Steps_params successfuly saved to {steps_params_path}')

    def unwrap_model(self):
        '''返回nn.Module模块
        '''
        if isinstance(self, nn.Module): return self
        return self.module if hasattr(self, 'module') else self


Trainer.compile_training_components = Trainer.compile


class TrainerDP(nn.DataParallel, Trainer):
    '''DataParallel模式使用多gpu的方法, 
    1) 父类顺序颠倒也会出问题
    2) 使用方式和nn.DataParallel一致, TrainerDP(net, *args, **kwargs)来使用
    '''
    def __init__(self, *args, **kwargs):
        Trainer.__init__(self)
        nn.DataParallel.__init__(self, *args, **kwargs)


class TrainerDDP(nn.parallel.DistributedDataParallel, Trainer):
    '''DistributedDataParallel模式使用多gpu的方法,
    1) 父类顺序颠倒也会出问题
    2) 使用方式和DistributedDataParallel一致, TrainerDDP(net, *args, **kwargs)来使用
    '''
    def __init__(self, *args, master_rank=0, **kwargs):
        Trainer.__init__(self)
        kwargs['device_ids'] = kwargs.get('device_ids', [int(os.getenv('LOCAL_RANK'))])
        kwargs['output_device'] = kwargs.get('output_device', int(os.getenv('LOCAL_RANK')))
        nn.parallel.DistributedDataParallel.__init__(self, *args, **kwargs)

        # 默认仅对master_rank=0打印信息
        assert isinstance(master_rank, (int, list, tuple)), 'Args `master_rank` only supoorts int, list, tuple'
        if isinstance(master_rank, int):
            master_rank = [master_rank]
        self.master_rank = master_rank
        self.verbose = (torch.distributed.get_rank() in master_rank)
    
    def _prepare_inputs(self, train_dataloader:DataLoader, steps_per_epoch:Union[int,None], epochs:int, verbose:int):
        # 如果使用ddp的时候没有使用DistributedSampler，这里会自动修改一下
        from torch.utils.data.distributed import DistributedSampler 
        if (train_dataloader.sampler is None) and (not isinstance(train_dataloader.sampler, DistributedSampler)):
            train_dataloader.sampler = DistributedSampler(train_dataloader.dataset)
        super()._prepare_inputs(train_dataloader, steps_per_epoch, epochs, verbose)
    
    def disable_workers_callback(self, callbacks: Union[Callback, List[Callback]]):
        '''非master_rank上不使用callback'''
        for callback in callbacks:
            if torch.distributed.get_rank() not in self.master_rank:
                callback.run_callback = False

    @classmethod
    def init_process_group(cls, master_rank=0, seed=42):
        '''初始化各项参数'''
        cls.ddp_config = DottableDict()
        cls.ddp_config.rank = int(os.environ["RANK"])
        cls.ddp_config.local_rank = int(os.getenv('LOCAL_RANK'))
        cls.ddp_config.device = torch.device('cuda', cls.ddp_config.local_rank)
        cls.ddp_config.world_size = int(os.environ["WORLD_SIZE"])
        cls.ddp_config.master_process = cls.ddp_config.rank == master_rank
        torch.cuda.set_device(cls.ddp_config.local_rank)
        seed_everything(seed + cls.ddp_config.rank)
        return cls.ddp_config


class AccelerateTrainer(Trainer):
    '''accelerate来训练'''
    def __init__(self, module: nn.Module, **configs):
        super().__init__(module)
        from accelerate import Accelerator
        accelerator = Accelerator(**configs)
        self.model = accelerator.prepare(module)
        self.accelerator = accelerator
        self.device = accelerator.device
        self.verbose = 1 if accelerator.is_local_main_process else 0
        log_warn('AcclerateTrainer may not be compatible with several callbacks, you may use custom callbacks instead.')
    
    def compile(self, *args, **kwargs):
        super().compile(*args, **kwargs)
        self.optimizer, self.scheduler, self.criterion = self.accelerator.prepare(self.optimizer, self.scheduler, self.criterion)

    def _prepare_inputs(self, train_dataloader:DataLoader, steps_per_epoch:Union[int,None], epochs:int, verbose:int):
        # 如果使用ddp的时候没有使用DistributedSampler，这里会自动修改一下
        train_dataloader = self.accelerator.prepare(train_dataloader)
        super()._prepare_inputs(train_dataloader, steps_per_epoch, epochs, verbose)

    def prepare(self, *args, **kwargs):
        '''调用acclerate的prepare, 如在外面评估时候需要对dev_dataloader使用'''
        return self.accelerator.prepare(*args, **kwargs)

    def unwrap_model(self):
        '''返回nn.Module模块'''
        unwrap_model = self.accelerator.unwrap_model(self.model)
        if isinstance(unwrap_model, nn.Module): return unwrap_model
        return unwrap_model.module if hasattr(unwrap_model, 'module') else unwrap_model

    def loss_backward(self, loss):
        self.accelerator.backward(loss)
        return loss


class DeepSpeedTrainer(Trainer):
    '''deepspeed来训练'''
    def __init__(self, module, verbose=0):
        super().__init__(module)
        self.model = module
        args = argument_parse()
        self.config = JsonConfig(args.deepspeed)
        self.set_default_args()  # 设置默认的一些参数
        self.trainer_config_process(self.config, auto_find_batch_size=False)  # 设置一些auto的参数

        if verbose > 0:
            log_info('Deepspeed config listed below.')
            print_table(json_flat(self.config), headers=['config_name', 'config_value'])
    
    def _prepare_inputs(self, train_dataloader:DataLoader, steps_per_epoch:Union[int,None], epochs:int, verbose:int):
        # batch_size需要使用deepspeed config中的train_batch_size/train_micro_batch_size_per_gpu
        if train_dataloader.batch_sampler is not None:
            btz = train_dataloader.batch_sampler.batch_size
            btz_ds = self.config.train_batch_size
            btz_ds_per = self.config.train_micro_batch_size_per_gpu
            if btz != btz_ds:
                log_warn_once(f'Use deepspeed config `train_batch_size`={btz_ds} and `train_micro_batch_size_per_gpu`={btz_ds_per} instead of `batch_size`={btz}')
            train_dataloader.batch_sampler.batch_size = self.config.train_batch_size
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
            "config_params": self.config,
            "optimizer": self.optimizer,
            "lr_scheduler": self.scheduler,
        }
        if self.config.get('zero_optimization', {}).get('offload_optimizer', {}).get('device') == 'cpu':
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

    def set_default_args(self):
        '''设置默认的参数，用于deepspeed里面参数设置为auto的情况'''
        self.config.steps_per_print = self.config.get('steps_per_print', 1e9)  # 默认不打印, 防止进度条打印问题

        self.config.world_size = int(os.environ["WORLD_SIZE"])
        self.config.per_device_train_batch_size = self.config.get('per_device_train_batch_size', 8)
        self.config.gradient_accumulation_steps = self.config.get('gradient_accumulation_steps', 1)
        self.config.max_grad_norm = self.config.get('max_grad_norm', 1.0)
        self.config.learning_rate = self.config.get('learning_rate', 5e-5)
        self.config.adam_beta1 = self.config.get('adam_beta1', 0.9)
        self.config.adam_beta2 = self.config.get('adam_beta2', 0.999)
        self.config.adam_epsilon = self.config.get('adam_epsilon', 1e-8)
        self.config.weight_decay = self.config.get('weight_decay', 0.0)
        self.config.fp16 = self.config.get('fp16', False)
        self.config.fp16_full_eval = self.config.get('fp16_full_eval', False)
        self.config.fp16_opt_level = self.config.get('fp16_opt_level', "O1")
        self.config.fp16_backend = self.config.get('fp16_backend', "auto")

        self.config.bf16 = self.config.get('bf16', False)
        self.config.bf16_full_eval = self.config.get('bf16_full_eval', False)
        self.config.warmup_steps = self.config.get('warmup_steps', 0)
        self.config.warmup_ratio = self.config.get('warmup_ratio', 0.0)

    def find_config_node(self, ds_key_long):
        config = self.config

        # find the config node of interest if it exists
        nodes = ds_key_long.split(".")
        ds_key = nodes.pop()
        for node in nodes:
            config = config.get(node)
            if config is None:
                return None, ds_key

        return config, ds_key
    
    def fill_match(self, ds_key_long, hf_val, must_match=True):
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
            config[ds_key] = hf_val
            return

        if not must_match:
            return

        ds_val = config.get(ds_key)
        if ds_val is not None and ds_val != hf_val:
            log_warn_once(f"- ds {ds_key_long}={ds_val} <> {hf_val}")

    def trainer_config_process(self, args, auto_find_batch_size=False):
        """自动填充和替换ds_config中的auto选项
        """
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
        hidden_size_auto_keys = [x for x in hidden_size_based_keys if self.is_auto(x)]

        if len(hidden_size_auto_keys) > 0:
            if hasattr(self.model.config, "hidden_size"):
                hidden_size = self.config.hidden_size
            elif hasattr(self.config, "hidden_sizes"):
                # if there are many hidden sizes pick the largest one
                hidden_size = max(self.config.hidden_sizes)
            else:
                raise ValueError(
                    "The model's config file has neither `hidden_size` nor `hidden_sizes` entry, "
                    "therefore it's not possible to automatically fill out the following `auto` entries "
                    f"in the DeepSpeed config file: {hidden_size_auto_keys}. You can fix that by replacing "
                    "`auto` values for these keys with an integer value of your choice."
                )

            self.fill_match("zero_optimization.reduce_bucket_size", hidden_size * hidden_size, must_match=False)
            _stage = self.find_config_node("zero_optimization.stage")
            if _stage[0] is not None and _stage[0].get(_stage[1]) == 3:
                # automatically assign the optimal config values based on model config
                self.fill_match("zero_optimization.stage3_prefetch_bucket_size", 0.9 * hidden_size * hidden_size, must_match=False)
                self.fill_match("zero_optimization.stage3_param_persistence_threshold", 10 * hidden_size, must_match=False)

        # scheduler
        if hasattr(self, 'totel_steps'):
            self.fill_match("scheduler.params.total_num_steps", self.total_steps)
            self.fill_match("scheduler.params.warmup_num_steps", (self.config.warmup_steps if self.config.warmup_steps > 0 
                                                                else math.ceil(self.total_steps * self.config.warmup_ratio)))


def add_trainer(obj, include=None, exclude=None, verbose=0, replace_func=False):
    '''为nn.Module添加Triner对应的方法'''
    if isinstance(obj, (Trainer, TrainerDP, TrainerDDP)):
        log_warn('obj is not a Trainer object')
        return obj
    elif not isinstance(obj, nn.Module):
        log_warn('obj is not a nn.Module object')
        return obj

    if include is None:
        include = set()
    elif isinstance(include, str):
        include = set([include])
    elif isinstance(include, (tuple, list)):
        include = set(include)
    else:
        raise TypeError(f'Arg `include` only receive str/list format, not {type(include)}')

    if exclude is None:
        exclude = set()
    elif isinstance(exclude, (tuple, list)):
        exclude = set(exclude)

    import types
    added_funcs = []
    for k in dir(Trainer):
        set_func = False
        if k in include:  # 必须包含的
            set_func = True
        elif k in exclude:  # 必须屏蔽的
            pass
        elif k.startswith('__') and k.endswith('__'):  # 内部函数不执行
            pass
        elif hasattr(obj, k):  # 如果下游重新定义, 则不继
            if replace_func:
                set_func = True
        else:
            set_func = True

        if set_func and eval(f'isfunction(Trainer.{k})'):
            exec(f'obj.{k} = types.MethodType(Trainer.{k}, obj)')
            added_funcs.append(k)
    obj.initialize()  # 这里初始化会得到一些其他的成员变量, 不可缺省

    if verbose and (len(added_funcs) > 0):
        log_info(f'Already add `{",".join(added_funcs)}` method')
    return obj


def add_module(obj, include=None, exclude=None, verbose=0, replace_func=False):
    '''为Trainer增加nn.Module的方法
    方便外部访问, 如obj.parameters()可以直接访问到obj.module.parameters()
    '''
    if isinstance(obj, nn.Module):
        return obj
    elif not isinstance(obj, Trainer):
        log_warn('obj is not a Trainer object')
        return obj
    elif not isinstance(obj.unwrap_model(), nn.Module):
        log_warn('obj.unwrap_model() is not a nn.Module object')
        return obj
    
    if include is None:
        include = set()
    elif isinstance(include, str):
        include = set([include])
    elif isinstance(include, (tuple, list)):
        include = set(include)
    else:
        raise TypeError(f'Arg `include` only receive str/list format, not {type(include)}')

    if exclude is None:
        exclude = set()
    elif isinstance(exclude, (tuple, list)):
        exclude = set(exclude)


    import types
    added_funcs = []
    for k in dir(obj.unwrap_model()):
        set_func = False
        if k in include:  # 必须包含的
            set_func = True
        elif k in exclude:  # 必须屏蔽的
            pass
        elif k.startswith('__') and k.endswith('__'):
            pass
        elif hasattr(obj, k):  # 如果下游重新定义, 则不继
            if replace_func:
                set_func = True
        else:
            set_func = True
        
        if set_func and eval(f'isinstance(obj.unwrap_model().{k}, types.MethodType)'):
            exec(f'obj.{k} = obj.unwrap_model().{k}')
            added_funcs.append(k)

    if verbose and (len(added_funcs) > 0):
        log_info(f'Already add `{",".join(added_funcs)}` method')
    return obj
