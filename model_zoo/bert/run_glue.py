# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import os
import random
import time

import numpy as np
import paddle
from datasets import load_dataset
from paddle.io import DataLoader
from paddle.metric import Accuracy

from paddlenlp.data import DataCollatorWithPadding
from paddlenlp.metrics import AccuracyAndF1, Mcc, PearsonAndSpearman
from paddlenlp.trainer.argparser import strtobool
from paddlenlp.transformers import (
    BertForSequenceClassification,
    BertTokenizer,
    ErnieForSequenceClassification,
    ErnieTokenizer,
    LinearDecayWithWarmup,
)

METRIC_CLASSES = {
    "cola": Mcc,
    "sst2": Accuracy,
    "mrpc": AccuracyAndF1,
    "stsb": PearsonAndSpearman,
    "qqp": AccuracyAndF1,
    "mnli": Accuracy,
    "qnli": Accuracy,
    "rte": Accuracy,
}

task_to_keys = {
    "cola": ("sentence", None),
    "mnli": ("premise", "hypothesis"),
    "mrpc": ("sentence1", "sentence2"),
    "qnli": ("question", "sentence"),
    "qqp": ("question1", "question2"),
    "rte": ("sentence1", "sentence2"),
    "sst2": ("sentence", None),
    "stsb": ("sentence1", "sentence2"),
    "wnli": ("sentence1", "sentence2"),
}

MODEL_CLASSES = {
    "bert": (BertForSequenceClassification, BertTokenizer),
    "ernie": (ErnieForSequenceClassification, ErnieTokenizer),
}


def parse_args():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument(
        "--task_name",
        default=None,
        type=str,
        required=True,
        help="The name of the task to train selected in the list: " + ", ".join(METRIC_CLASSES.keys()),
    )
    parser.add_argument(
        "--model_type",
        default=None,
        type=str,
        required=True,
        help="Model type selected in the list: " + ", ".join(MODEL_CLASSES.keys()),
    )
    parser.add_argument(
        "--model_name_or_path",
        default=None,
        type=str,
        required=True,
        help="Path to pre-trained model or shortcut name selected in the list: "
        + ", ".join(
            sum([list(classes[-1].pretrained_init_configuration.keys()) for classes in MODEL_CLASSES.values()], [])
        ),
    )
    parser.add_argument(
        "--output_dir",
        default=None,
        type=str,
        required=True,
        help="The output directory where the model predictions and checkpoints will be written.",
    )
    parser.add_argument(
        "--max_seq_length",
        default=128,
        type=int,
        help="The maximum total input sequence length after tokenization. Sequences longer "
        "than this will be truncated, sequences shorter will be padded.",
    )
    parser.add_argument("--learning_rate", default=1e-4, type=float, help="The initial learning rate for Adam.")
    parser.add_argument(
        "--num_train_epochs",
        default=3,
        type=int,
        help="Total number of training epochs to perform.",
    )
    parser.add_argument("--logging_steps", type=int, default=100, help="Log every X updates steps.")
    parser.add_argument("--save_steps", type=int, default=100, help="Save checkpoint every X updates steps.")
    parser.add_argument(
        "--batch_size",
        default=32,
        type=int,
        help="Batch size per GPU/CPU for training.",
    )
    parser.add_argument("--weight_decay", default=0.0, type=float, help="Weight decay if we apply some.")
    parser.add_argument(
        "--warmup_steps",
        default=0,
        type=int,
        help="Linear warmup over warmup_steps. If > 0: Override warmup_proportion",
    )
    parser.add_argument(
        "--warmup_proportion", default=0.1, type=float, help="Linear warmup proportion over total steps."
    )
    parser.add_argument("--adam_epsilon", default=1e-6, type=float, help="Epsilon for Adam optimizer.")
    parser.add_argument(
        "--max_steps",
        default=-1,
        type=int,
        help="If > 0: set total number of training steps to perform. Override num_train_epochs.",
    )
    parser.add_argument("--seed", default=42, type=int, help="random seed for initialization")
    parser.add_argument(
        "--device",
        default="gpu",
        type=str,
        choices=["cpu", "gpu", "xpu", "npu"],
        help="The device to select to train the model, is must be cpu/gpu/xpu/npu.",
    )
    parser.add_argument("--use_amp", type=strtobool, default=False, help="Enable mixed precision training.")
    parser.add_argument("--scale_loss", type=float, default=2**15, help="The value of scale_loss for fp16.")
    args = parser.parse_args()
    return args


def set_seed(args):
    # Use the same data seed(for data shuffle) for all procs to guarantee data
    # consistency after sharding.
    random.seed(args.seed)
    np.random.seed(args.seed)
    # Maybe different op seeds(for dropout) for different procs is better. By:
    # `paddle.seed(args.seed + paddle.distributed.get_rank())`
    paddle.seed(args.seed)


@paddle.no_grad()
def evaluate(model, loss_fct, metric, data_loader):
    model.eval()
    metric.reset()
    for batch in data_loader:
        logits = model(batch["input_ids"], batch["token_type_ids"])
        loss = loss_fct(logits, batch["labels"])
        correct = metric.compute(logits, batch["labels"])
        metric.update(correct)
    res = metric.accumulate()
    if isinstance(metric, AccuracyAndF1):
        print(
            "eval loss: %f, acc: %s, precision: %s, recall: %s, f1: %s, acc and f1: %s, "
            % (
                loss.numpy(),
                res[0],
                res[1],
                res[2],
                res[3],
                res[4],
            ),
            end="",
        )
    elif isinstance(metric, Mcc):
        print("eval loss: %f, mcc: %s, " % (loss.numpy(), res[0]), end="")
    elif isinstance(metric, PearsonAndSpearman):
        print(
            "eval loss: %f, pearson: %s, spearman: %s, pearson and spearman: %s, "
            % (loss.numpy(), res[0], res[1], res[2]),
            end="",
        )
    else:
        print("eval loss: %f, acc: %s, " % (loss.numpy(), res), end="")
    model.train()


def do_train(args):
    paddle.set_device(args.device)
    if paddle.distributed.get_world_size() > 1:
        paddle.distributed.init_parallel_env()

    set_seed(args)

    args.task_name = args.task_name.lower()

    sentence1_key, sentence2_key = task_to_keys[args.task_name]

    metric_class = METRIC_CLASSES[args.task_name]
    args.model_type = args.model_type.lower()
    model_class, tokenizer_class = MODEL_CLASSES[args.model_type]

    train_ds = load_dataset("glue", args.task_name, split="train")
    columns = train_ds.column_names
    is_regression = args.task_name == "stsb"
    label_list = None
    if not is_regression:
        label_list = train_ds.features["label"].names
        num_classes = len(label_list)
    else:
        num_classes = 1
    tokenizer = tokenizer_class.from_pretrained(args.model_name_or_path)

    def preprocess_function(examples):
        # Tokenize the texts
        texts = (
            (examples[sentence1_key],) if sentence2_key is None else (examples[sentence1_key], examples[sentence2_key])
        )
        result = tokenizer(*texts, max_seq_len=args.max_seq_length)
        if "label" in examples:
            # In all cases, rename the column to labels because the model will expect that.
            result["labels"] = examples["label"]
        return result

    train_ds = train_ds.map(preprocess_function, batched=True, remove_columns=columns)
    train_batch_sampler = paddle.io.DistributedBatchSampler(train_ds, batch_size=args.batch_size, shuffle=True)
    batchify_fn = DataCollatorWithPadding(tokenizer)
    train_data_loader = DataLoader(
        dataset=train_ds, batch_sampler=train_batch_sampler, collate_fn=batchify_fn, num_workers=0, return_list=True
    )
    if args.task_name == "mnli":
        dev_ds_matched, dev_ds_mismatched = load_dataset(
            "glue", args.task_name, split=["validation_matched", "validation_mismatched"]
        )

        dev_ds_matched = dev_ds_matched.map(preprocess_function, batched=True, remove_columns=columns)
        dev_ds_mismatched = dev_ds_mismatched.map(preprocess_function, batched=True, remove_columns=columns)
        dev_batch_sampler_matched = paddle.io.BatchSampler(dev_ds_matched, batch_size=args.batch_size, shuffle=False)
        dev_data_loader_matched = DataLoader(
            dataset=dev_ds_matched,
            batch_sampler=dev_batch_sampler_matched,
            collate_fn=batchify_fn,
            num_workers=0,
            return_list=True,
        )
        dev_batch_sampler_mismatched = paddle.io.BatchSampler(
            dev_ds_mismatched, batch_size=args.batch_size, shuffle=False
        )
        dev_data_loader_mismatched = DataLoader(
            dataset=dev_ds_mismatched,
            batch_sampler=dev_batch_sampler_mismatched,
            collate_fn=batchify_fn,
            num_workers=0,
            return_list=True,
        )
    else:
        dev_ds = load_dataset("glue", args.task_name, split="validation")
        dev_ds = dev_ds.map(preprocess_function, batched=True, remove_columns=columns)
        dev_batch_sampler = paddle.io.BatchSampler(dev_ds, batch_size=args.batch_size, shuffle=False)
        dev_data_loader = DataLoader(
            dataset=dev_ds, batch_sampler=dev_batch_sampler, collate_fn=batchify_fn, num_workers=0, return_list=True
        )

    model = model_class.from_pretrained(args.model_name_or_path, num_classes=num_classes)
    if paddle.distributed.get_world_size() > 1:
        model = paddle.DataParallel(model)

    num_training_steps = args.max_steps if args.max_steps > 0 else (len(train_data_loader) * args.num_train_epochs)
    warmup = args.warmup_steps if args.warmup_steps > 0 else args.warmup_proportion

    lr_scheduler = LinearDecayWithWarmup(args.learning_rate, num_training_steps, warmup)

    # Generate parameter names needed to perform weight decay.
    # All bias and LayerNorm parameters are excluded.
    decay_params = [p.name for n, p in model.named_parameters() if not any(nd in n for nd in ["bias", "norm"])]
    optimizer = paddle.optimizer.AdamW(
        learning_rate=lr_scheduler,
        beta1=0.9,
        beta2=0.999,
        epsilon=args.adam_epsilon,
        parameters=model.parameters(),
        weight_decay=args.weight_decay,
        apply_decay_param_fun=lambda x: x in decay_params,
    )

    loss_fct = paddle.nn.loss.CrossEntropyLoss() if not is_regression else paddle.nn.loss.MSELoss()

    metric = metric_class()
    if args.use_amp:
        scaler = paddle.amp.GradScaler(init_loss_scaling=args.scale_loss)

    global_step = 0
    tic_train = time.time()
    for epoch in range(args.num_train_epochs):
        for step, batch in enumerate(train_data_loader):
            global_step += 1
            with paddle.amp.auto_cast(args.use_amp, custom_white_list=["layer_norm", "softmax", "gelu"]):
                logits = model(batch["input_ids"], batch["token_type_ids"])
                loss = loss_fct(logits, batch["labels"])
            if args.use_amp:
                scaler.scale(loss).backward()
                scaler.minimize(optimizer, loss)
            else:
                loss.backward()
                optimizer.step()
            lr_scheduler.step()
            optimizer.clear_grad()
            if global_step % args.logging_steps == 0:
                print(
                    "global step %d/%d, epoch: %d, batch: %d, rank_id: %s, loss: %f, lr: %.10f, speed: %.4f step/s"
                    % (
                        global_step,
                        num_training_steps,
                        epoch,
                        step,
                        paddle.distributed.get_rank(),
                        loss,
                        optimizer.get_lr(),
                        args.logging_steps / (time.time() - tic_train),
                    )
                )
                tic_train = time.time()
            if global_step % args.save_steps == 0 or global_step == num_training_steps:
                tic_eval = time.time()
                if args.task_name == "mnli":
                    evaluate(model, loss_fct, metric, dev_data_loader_matched)
                    evaluate(model, loss_fct, metric, dev_data_loader_mismatched)
                    print("eval done total : %s s" % (time.time() - tic_eval))
                else:
                    evaluate(model, loss_fct, metric, dev_data_loader)
                    print("eval done total : %s s" % (time.time() - tic_eval))
                if paddle.distributed.get_rank() == 0:
                    output_dir = os.path.join(
                        args.output_dir, "%s_ft_model_%d.pdparams" % (args.task_name, global_step)
                    )
                    if not os.path.exists(output_dir):
                        os.makedirs(output_dir)
                    # Need better way to get inner model of DataParallel
                    model_to_save = model._layers if isinstance(model, paddle.DataParallel) else model
                    model_to_save.save_pretrained(output_dir)
                    tokenizer.save_pretrained(output_dir)
            if global_step >= num_training_steps:
                return


def print_arguments(args):
    """print arguments"""
    print("-----------  Configuration Arguments -----------")
    for arg, value in sorted(vars(args).items()):
        print("%s: %s" % (arg, value))
    print("------------------------------------------------")


if __name__ == "__main__":
    args = parse_args()
    print_arguments(args)
    do_train(args)
