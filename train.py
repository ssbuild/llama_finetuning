# -*- coding: utf-8 -*-
import copy
import os

import numpy as np
import torch
from deep_training.data_helper import ModelArguments, DataArguments, TrainingArguments
from deep_training.nlp.models.LLaMA_parallel import TransformerLLaMALMHeadModel, LLaMAConfig, setup_model_parallel
from deep_training.utils.trainer import SimpleModelCheckpoint
from pytorch_lightning import Trainer
from transformers import HfArgumentParser

from data_utils import NN_DataHelper, train_info_args, preprocess, postprocess
from sentencepiece_tokenizer import SentencePieceTokenizer


class MyTransformer(TransformerLLaMALMHeadModel, with_pl=True):
    def __init__(self, *args, **kwargs):
        super(MyTransformer, self).__init__(*args, **kwargs)



class MySimpleModelCheckpoint(SimpleModelCheckpoint):
    def __init__(self, *args, **kwargs):
        super(MySimpleModelCheckpoint, self).__init__(*args, **kwargs)

        self.output_dir = './best_ckpt'
        if not os.path.exists(self.output_dir):
            os.mkdir(self.output_dir)
        self.weight_file = os.path.join(self.output_dir ,'best.pt')

        self.save_flag = False

    @staticmethod
    def generate_text(pl_module: MyTransformer, prompt_text, tokenizer: SentencePieceTokenizer, max_target_length, device=0):
        device = torch.device('cuda:{}'.format(device))
        # 简易测试生成
        input_ids = tokenizer.encode(prompt_text, eos=False)
        gen_ids, gen_tokens = [], []
        batch = {}
        for i in range(max_target_length):
            batch.clear()
            batch['input_ids'] = [input_ids + gen_ids]
            for k in batch:
                batch[k] = torch.tensor(batch[k], dtype=torch.int32,device=device)

            out = pl_module.test_step(batch, 0)
            logits = out['outputs'][0]
            logits = np.argmax(logits[:, -1], axis=-1)
            logits = logits[0].tolist()
            gen_ids.append(logits)
            token = tokenizer.decode([logits])
            gen_tokens.append(token)

        out_text = ''.join(gen_tokens)
        out_text = postprocess(out_text)
        return out_text




    def on_save_model(
            self, trainer: "pl.Trainer", pl_module: "pl.LightningModule"
    ) -> None:
        # 保存权重
        super(MySimpleModelCheckpoint, self).on_save_model(trainer, pl_module)

        if not self.save_flag:
            self.save_flag = True
            config : LLaMAConfig = pl_module.config
            config = copy.deepcopy(config)
            config.save_pretrained(self.output_dir)

        prefixs = [
            "帮我写一个请假条，我因为新冠不舒服，需要请假3天，请领导批准",
            "你能干什么",
            "I believe the meaning of life is",
            "Simply put, the theory of relativity states that ",
            "Building a website can be done in 10 simple steps:\n",

        ]

        device = trainer.global_rank
        self.tokenizer: SentencePieceTokenizer
        tokenizer = self.tokenizer
        data_args = self.data_args

        print('*' * 30, 'generate_text...')
        for text in prefixs:
            input_text = "用户：" + text + "\n小元："
            input_text = preprocess(input_text)
            output = MySimpleModelCheckpoint.generate_text(pl_module, input_text, tokenizer,
                                                           data_args.max_target_length, device=device)

            print('input', text)
            print('output', output)
            print()

if __name__ == '__main__':

    parser = HfArgumentParser((ModelArguments, TrainingArguments, DataArguments))
    model_args, training_args, data_args = parser.parse_dict(train_info_args)

    # 并行
    setup_model_parallel()
    # 保存最小loss模型
    checkpoint_callback = MySimpleModelCheckpoint(
        # monitor="loss",
        every_n_epochs=1,
                                                  every_n_train_steps=2000 // training_args.gradient_accumulation_steps)
    trainer = Trainer(
        callbacks=[checkpoint_callback],
        max_epochs=training_args.max_epochs,
        max_steps=training_args.max_steps,
        accelerator="gpu",replace_sampler_ddp=False,
        devices=data_args.devices,
        enable_progress_bar=True,
        default_root_dir=data_args.output_dir,
        gradient_clip_val=training_args.max_grad_norm,
        accumulate_grad_batches=training_args.gradient_accumulation_steps,
        num_sanity_val_steps=0,
        strategy='ddp' if torch.cuda.device_count() > 1 else None,
    )

    dataHelper = NN_DataHelper(model_args, training_args, data_args)

    tokenizer, config, label2id, id2label = dataHelper.load_tokenizer_and_config(tokenizer_class_name=SentencePieceTokenizer,config_class_name=LLaMAConfig)


    # 额外参数
    checkpoint_callback.tokenizer = tokenizer
    checkpoint_callback.data_args = data_args

    # 缓存数据集
    if data_args.do_train:
        dataHelper.make_dataset_with_args(data_args.train_file,mixed_data=False,shuffle=True,mode='train')
    if data_args.do_eval:
        dataHelper.make_dataset_with_args(data_args.eval_file, mode='eval')
    if data_args.do_test:
        dataHelper.make_dataset_with_args(data_args.test_file,mode='test')


    model = MyTransformer(config=config, model_args=model_args, training_args=training_args)

    ckpt_path = './best_ckpt/best.pt'
    if not data_args.convert_onnx:
        # if os.path.exists(ckpt_path):
        #     # 加载权重继续训练
        #     model = MyTransformer.load_from_checkpoint(ckpt_path, config=config,
        #                                                model_args=model_args,
        #                                                training_args=training_args)

        train_datasets = dataHelper.load_random_sampler(dataHelper.train_files,
                                                        with_load_memory=True,
                                                        collate_fn=dataHelper.collate_fn,
                                                        batch_size=training_args.train_batch_size,
                                                        shuffle=True,infinite=True,num_processes=trainer.world_size,process_index=trainer.global_rank)

        if train_datasets is not None:
            trainer.fit(model, train_dataloaders=train_datasets)
        # else:
        #     eval_datasets = dataHelper.load_sequential_sampler(dataHelper.eval_files,batch_size=training_args.eval_batch_size,collate_fn=dataHelper.collate_fn)
        #     test_datasets = dataHelper.load_sequential_sampler(dataHelper.test_files,batch_size=training_args.test_batch_size,collate_fn=dataHelper.collate_fn)
        #
        #     if eval_datasets is not None:
        #         trainer.validate(model, dataloaders=eval_datasets, ckpt_path='./best.pt')
        #     if test_datasets is not None:
        #         trainer.test(model, dataloaders=test_datasets, ckpt_path='best.pt')
    else:
        # 加载权重
        model = MyTransformer.load_from_checkpoint(ckpt_path, config=config,
                                                       model_args=model_args,
                                                       training_args=training_args)
        input_sample = (
            ("input_ids", torch.ones(size=(1, 128), dtype=torch.int32)),
        )
        input_names = ("input_ids",)
        output_names = ("pred_ids",)
        dynamic_axes = None or {"input_ids": [0, 1],
                                "pred_ids": [0, 1]}
        model.convert_to_onnx('./best_ckpt/best.onnx',
                              input_sample=input_sample,
                              input_names=input_names,
                              output_names=output_names,
                              dynamic_axes=dynamic_axes)