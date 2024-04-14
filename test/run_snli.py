#  Copyright (c) 2020.
#
#  Author: Yannik Benz

import collections
import glob
import math
import os
import re

import numpy as np
import tensorflow as tf
from absl import flags, app, logging
from seqeval import metrics
from sklearn.metrics import classification_report
from transformers import (
    TF2_WEIGHTS_NAME,
    BertConfig,
    BertTokenizer,
    DistilBertConfig,
    DistilBertTokenizer,
    RobertaConfig,
    RobertaTokenizer,
    TFBertForSequenceClassification,
    TFDistilBertForSequenceClassification,
    TFRobertaForSequenceClassification,
    create_optimizer, GradientAccumulator)

import zeroe.utils.snli_utils as utils

try:
    from fastprogress import master_bar, progress_bar
except ImportError:
    from fastprogress.fastprogress import master_bar, progress_bar

ALL_MODELS = sum(
    (tuple(conf.pretrained_config_archive_map.keys()) for conf in (BertConfig, RobertaConfig, DistilBertConfig)), ()
)

MODEL_CLASSES = {
    "bert": (BertConfig, TFBertForSequenceClassification, BertTokenizer),
    "roberta": (RobertaConfig, TFRobertaForSequenceClassification, RobertaTokenizer),
    "distilbert": (DistilBertConfig, TFDistilBertForSequenceClassification, DistilBertTokenizer),
}

flags.DEFINE_string(
    "data_dir", None, "The input data dir. Should contain the .conll files (or other data files) " "for the task."
)

flags.DEFINE_string("model_type", None, "Model type selected in the list: " + ", ".join(MODEL_CLASSES.keys()))

flags.DEFINE_string("output_dir", None, "The output directory where the model checkpoints will be written.")

flags.DEFINE_string(
    "model_name_or_path",
    None,
    "Path to pre-trained model or shortcut name selected in the list: " + ", ".join(ALL_MODELS),
)

flags.DEFINE_string(
    "labels", "", "Path to a file containing all labels. If not specified, CoNLL-2003 labels are used."
)

flags.DEFINE_string("config_name", "", "Pretrained config name or path if not the same as model_name")

flags.DEFINE_string("tokenizer_name", "", "Pretrained tokenizer name or path if not the same as model_name")

flags.DEFINE_string("cache_dir", "", "Where do you want to store the pre-trained models downloaded from s3")

flags.DEFINE_integer(
    "max_seq_length",
    128,
    "The maximum total input sentence length after tokenization. "
    "Sequences longer than this will be truncated, sequences shorter "
    "will be padded.",
)

PERTURBER = ["full-swap", "inner-swap", "intrude", "disemvowel",
             "truncate", "keyboard-typo", "natural-typo", "segment",
             "phonetic", "viper"]
NO_PERTURBER = ["no_" + pert for pert in PERTURBER]
flags.DEFINE_string(
    "perturber",
    None,
    "The actual perturber data to apply."
)
flags.register_validator("perturber",
                         lambda flag_value: flag_value is None or flag_value in PERTURBER + NO_PERTURBER,
                         f"Perturber must be in {PERTURBER + NO_PERTURBER}")

LEVELS = ["low", "mid", "high", "lmh", "clmh"]
flags.DEFINE_string(
    "level",
    None,
    "The level of perturbation to take."
)
flags.register_validator("level",
                         lambda flag_value: flag_value is None or flag_value in LEVELS,
                         f"Level must be in {LEVELS}")

flags.DEFINE_string(
    "tpu",
    None,
    "The Cloud TPU to use for training. This should be either the name "
    "used when creating the Cloud TPU, or a grpc://ip.address.of.tpu:8470 "
    "url.",
)

flags.DEFINE_integer("num_tpu_cores", 8, "Total number of TPU cores to use.")

flags.DEFINE_boolean("do_train", False, "Whether to run training.")

flags.DEFINE_boolean("do_eval", False, "Whether to run eval on the dev set.")

flags.DEFINE_boolean("do_predict", False, "Whether to run predictions on the test set.")

flags.DEFINE_boolean(
    "evaluate_during_training", False, "Whether to run evaluation during training at each logging step."
)

flags.DEFINE_boolean("do_lower_case", False, "Set this flag if you are using an uncased model.")

flags.DEFINE_integer("per_device_train_batch_size", 8, "Batch size per GPU/CPU/TPU for training.")

flags.DEFINE_integer("per_device_eval_batch_size", 8, "Batch size per GPU/CPU/TPU for evaluation.")

flags.DEFINE_integer(
    "gradient_accumulation_steps", 1, "Number of updates steps to accumulate before performing a backward/update pass."
)

flags.DEFINE_float("learning_rate", 3e-5, "The initial learning rate for Adam.")

flags.DEFINE_float("weight_decay", 0.0, "Weight decay if we apply some.")

flags.DEFINE_float("adam_epsilon", 1e-8, "Epsilon for Adam optimizer.")

flags.DEFINE_float("max_grad_norm", 1.0, "Max gradient norm.")

flags.DEFINE_integer("num_train_epochs", 3, "Total number of training epochs to perform.")

flags.DEFINE_integer(
    "max_steps", -1, "If > 0: set total number of training steps to perform. Override num_train_epochs."
)

flags.DEFINE_integer("warmup_steps", 0, "Linear warmup over warmup_steps.")

flags.DEFINE_integer("logging_steps", 50, "Log every X updates steps.")

flags.DEFINE_integer("save_steps", 0, "Save checkpoint every X updates steps.")

flags.DEFINE_boolean(
    "eval_all_checkpoints",
    False,
    "Evaluate all checkpoints starting with the same prefix as model_name ending and ending with step number",
)

flags.DEFINE_boolean("no_cuda", False, "Avoid using CUDA when available")

flags.DEFINE_boolean("overwrite_output_dir", False, "Overwrite the content of the output directory")

flags.DEFINE_boolean("overwrite_cache", False, "Overwrite the cached training and evaluation sets")

flags.DEFINE_integer("seed", 42, "random seed for initialization")

flags.DEFINE_boolean("fp16", False, "Whether to use 16-bit (mixed) precision instead of 32-bit")

flags.DEFINE_string(
    "gpus",
    "0",
    "Comma separated list of gpus devices. If only one, switch to single "
    "gpu strategy, if None takes all the gpus available.",
)


def evaluate(args, strategy, model, tokenizer, labels, pad_token_label_id, mode):
    eval_batch_size = args["per_device_eval_batch_size"] * args["n_device"]
    eval_dataset, size = load_and_cache_examples(
        args, tokenizer, labels, pad_token_label_id, eval_batch_size, mode=mode
    )
    eval_dataset = strategy.experimental_distribute_dataset(eval_dataset)
    preds = None
    num_eval_steps = math.ceil(size / eval_batch_size)
    master = master_bar(range(1))
    eval_iterator = progress_bar(eval_dataset, total=num_eval_steps, parent=master, display=args["n_device"] > 1)
    loss_fct = tf.keras.losses.SparseCategoricalCrossentropy(reduction=tf.keras.losses.Reduction.NONE)
    loss = 0.0

    logging.info("***** Running evaluation *****")
    logging.info("  Num examples = %d", size)
    logging.info("  Batch size = %d", eval_batch_size)
    logging.info("  Perturber = %s", args["perturber"] if args["perturber"] else "None")
    if args["level"]:
        logging.info("  Perturbation Level = %s", args["level"])

    for eval_features, eval_labels in eval_iterator:
        inputs = {
            "attention_mask": eval_features["attention_mask"],
            "training": False
        }

        if args["model_type"] != "distilbert":
            inputs["token_type_ids"] = (
                eval_features["token_type_ids"] if args["model_type"] in ["bert", "xlnet"] else None
            )

        with strategy.scope():
            logits = model(eval_features["input_ids"], **inputs)[0]
            logits = tf.reshape(logits, (-1, len(labels)))
            cross_entropy = loss_fct(eval_labels, logits)
            loss += tf.reduce_sum(cross_entropy) * (1.0 / eval_batch_size)

        if preds is None:
            preds = logits.numpy()
            label_ids = eval_labels.numpy()
        else:
            preds = np.append(preds, logits.numpy(), axis=0)
            label_ids = np.append(label_ids, eval_labels.numpy(), axis=0)

    pred_label_ids = np.argmax(preds, axis=1)
    y_pred = []
    y_true = []
    for pred, true in zip(pred_label_ids, label_ids.reshape((-1,))):
        y_pred.append(pred)
        y_true.append(true)
    loss = loss / num_eval_steps

    return y_true, y_pred, loss.numpy()


def train(
        args, strategy, train_dataset, tokenizer, model, num_train_examples, labels, train_batch_size,
        pad_token_label_id
):
    """
    TODO: doc

    :param strategy:
    :param args:
    :param train_dataset:
    :param tokenizer:
    :param model:
    :param num_train_examples:
    :param labels:
    :param train_batch_size:
    :param pad_token_label_id:
    :return:
    """
    if args["max_steps"] > 0:
        num_train_steps = args["max_steps"] * args["gradient_accumulation_steps"]
        args["num_train_epochs"] = 1
    else:
        num_train_steps = (
                math.ceil(num_train_examples / train_batch_size)
                // args["gradient_accumulation_steps"]
                * args["num_train_epochs"]
        )

    writer = tf.summary.create_file_writer("../../../tmp/mylogs")

    with strategy.scope():
        loss_fct = tf.keras.losses.SparseCategoricalCrossentropy(reduction=tf.keras.losses.Reduction.NONE)
        optimizer = create_optimizer(args["learning_rate"], num_train_steps, args["warmup_steps"])

        if args["fp16"]:
            optimizer = tf.keras.mixed_precision.experimental.LossScaleOptimizer(optimizer, "dynamic")

        loss_metric = tf.keras.metrics.Mean(name="loss", dtype=tf.float32)
        gradient_accumulator = GradientAccumulator()

    logging.info("***** Running training *****")
    logging.info("  Num examples = %d", num_train_examples)
    logging.info("  Num Epochs = %d", args["num_train_epochs"])
    logging.info("  Instantaneous batch size per device = %d", args["per_device_train_batch_size"])
    logging.info(
        "  Total train batch size (w. parallel, distributed & accumulation) = %d",
        train_batch_size * args["gradient_accumulation_steps"],
    )
    logging.info("  Gradient Accumulation steps = %d", args["gradient_accumulation_steps"])
    logging.info("  Total training steps = %d", num_train_steps)
    logging.info("  Perturber = %s", args["perturber"] if args["perturber"] else "None")
    if args["level"]:
        logging.info("  Perturbation Level = %s", args["level"])

    model.summary()

    @tf.function
    def apply_gradients():
        """
        TODO: doc

        :return:
        """
        grads_and_vars = []

        for gradient, variable in zip(gradient_accumulator.gradients, model.trainable_variables):
            if gradient is not None:
                scaled_gradient = gradient / (args["n_device"] * args["gradient_accumulation_steps"])
                grads_and_vars.append((scaled_gradient, variable))
            else:
                grads_and_vars.append((gradient, variable))

        optimizer.apply_gradients(grads_and_vars, args["max_grad_norm"])
        gradient_accumulator.reset()

    @tf.function
    def train_step(train_features, train_labels):
        """
        TODO: doc

        :param train_features:
        :param train_labels:
        :return:
        """

        def step_fn(train_features, train_labels):
            """
            TODO: doc

            :param train_features:
            :param train_labels:
            :return:
            """
            inputs = {
                "attention_mask": train_features["attention_mask"],
                "training": True
            }

            if args["model_type"] != "distilbert":
                inputs["token_type_ids"] = (
                    train_features["token_type_ids"] if args["model_type"] in ["bert", "xlnet"] else None
                )

            with tf.GradientTape() as tape:
                logits = model(train_features["input_ids"], **inputs)[0]
                logits = tf.reshape(logits, (-1, len(labels)))
                cross_entropy = loss_fct(train_labels, logits)
                loss = tf.reduce_sum(cross_entropy) * (1.0 / train_batch_size)
                grads = tape.gradient(loss, model.trainable_variables)

                gradient_accumulator(grads)

            return cross_entropy

        per_example_losses = strategy.experimental_run_v2(step_fn, args=(train_features, train_labels))
        mean_loss = strategy.reduce(tf.distribute.ReduceOp.MEAN, per_example_losses, axis=0)

        return mean_loss

    # current_time = datetime.datetime.now()
    train_iterator = master_bar(range(args["num_train_epochs"]))
    global_step = 0
    logging_loss = 0.0

    for epoch in train_iterator:
        epoch_iterator = progress_bar(
            train_dataset, total=num_train_steps, parent=train_iterator, display=args["n_device"] > 1
        )
        step = 1

        with strategy.scope():
            for train_features, train_labels in epoch_iterator:
                loss = train_step(train_features, train_labels)

                if step % args["gradient_accumulation_steps"] == 0:
                    strategy.experimental_run_v2(apply_gradients)

                    loss_metric(loss)

                    global_step += 1

                    if args["logging_steps"] > 0 and global_step % args["logging_steps"] == 0:
                        # Log metrics
                        if args["n_device"] == 1 and args["evaluate_during_training"]:
                            y_true, y_pred, eval_loss = evaluate(
                                args, strategy, model, tokenizer, labels, pad_token_label_id, mode="dev"
                            )
                            report = metrics.classification_report(y_true, y_pred, digits=4)

                            logging.info("Eval at step " + str(global_step) + "\n" + report)
                            logging.info("eval_loss: " + str(eval_loss))

                            accuracy = metrics.accuracy_score(y_true, y_pred)
                            precision = metrics.precision_score(y_true, y_pred)
                            recall = metrics.recall_score(y_true, y_pred)
                            f1 = metrics.f1_score(y_true, y_pred)

                            logging.info("eval_accuracy : " + str(accuracy))

                            with writer.as_default():
                                tf.summary.scalar("eval_loss", eval_loss, global_step)
                                tf.summary.scalar("accuracy", accuracy, global_step)
                                tf.summary.scalar("precision", precision, global_step)
                                tf.summary.scalar("recall", recall, global_step)
                                tf.summary.scalar("f1", f1, global_step)

                        lr = optimizer.learning_rate
                        learning_rate = lr(step)

                        with writer.as_default():
                            tf.summary.scalar("lr", learning_rate, global_step)
                            tf.summary.scalar(
                                "loss", (loss_metric.result() - logging_loss) / args["logging_steps"], global_step
                            )

                        logging_loss = loss_metric.result()

                    with writer.as_default():
                        tf.summary.scalar("loss", loss_metric.result(), step=step)

                    if args["save_steps"] > 0 and global_step % args["save_steps"] == 0:
                        # Save model checkpoint
                        output_dir = os.path.join(args["output_dir"], "checkpoint-{}".format(global_step))

                        if not os.path.exists(output_dir):
                            os.makedirs(output_dir)

                        model.save_pretrained(output_dir)
                        logging.info("Saving model checkpoint to %s", output_dir)

                train_iterator.child.comment = f"loss : {loss_metric.result()}"
                step += 1

        train_iterator.write(f"loss epoch {epoch + 1}: {loss_metric.result()}")

        loss_metric.reset_states()

    logging.info("Training took time = {}".format("TODO"))


def load_cache(cached_file, max_seq_length):
    """
    TODO: doc

    :param cached_file:
    :param max_seq_length:
    :return:
    """
    name_to_features = {
        "input_ids": tf.io.FixedLenFeature([max_seq_length], tf.int64),
        "attention_mask": tf.io.FixedLenFeature([max_seq_length], tf.int64),
        "token_type_ids": tf.io.FixedLenFeature([max_seq_length], tf.int64),
        "label": tf.io.FixedLenFeature([1], tf.int64),
    }

    def _decode_record(record):
        """
        TODO: doc

        :param record:
        :return:
        """
        example = tf.io.parse_single_example(record, name_to_features)
        features = {
            "input_ids": example["input_ids"],
            "attention_mask": example["attention_mask"],
            "token_type_ids": example["token_type_ids"]
        }

        return features, example["label"]

    d = tf.data.TFRecordDataset(cached_file)
    d = d.map(_decode_record, num_parallel_calls=4)
    count = d.reduce(0, lambda x, _: x + 1)

    return d, count.numpy()


def save_cache(features, cached_features_file):
    """
    TODO: doc

    :param features:
    :param cached_features_file:
    :return:
    """
    writer = tf.io.TFRecordWriter(cached_features_file)

    for (ex_index, feature) in enumerate(features):
        if ex_index % 5000 == 0:
            logging.info("Writing example %d of %d" % (ex_index, len(features)))

        def create_int_feature(values):
            f = tf.train.Feature(int64_list=tf.train.Int64List(value=list(values)))
            return f

        record_feature = collections.OrderedDict()
        record_feature["input_ids"] = create_int_feature(feature.input_ids)
        record_feature["attention_mask"] = create_int_feature(feature.attention_mask)
        record_feature["token_type_ids"] = create_int_feature(feature.token_type_ids)
        record_feature["label"] = create_int_feature([feature.label])

        tf_example = tf.train.Example(features=tf.train.Features(feature=record_feature))

        writer.write(tf_example.SerializeToString())

    writer.close()


def load_and_cache_examples(args, tokenizer, labels, pad_token_label_id, batch_size, mode):
    """

    :param args:
    :param tokenizer:
    :param labels:
    :param pad_token_label_id:
    :param batch_size:
    :param mode:
    :return:
    """
    drop_remainder = True if args["tpu"] or mode == "train" else False
    # Load data features from cache or dataset file
    if args["perturber"] and args["level"]:
        cached_features_file = os.path.join(
            args["data_dir"],
            "cached_{}_{}_{}_{}_{}.tf_record".format(
                mode,
                args["perturber"],
                args["level"],
                list(filter(None, args["model_name_or_path"].split("/"))).pop(),
                str(args["max_seq_length"])
            ),
        )
    else:
        cached_features_file = os.path.join(
            args["data_dir"],
            "cached_clean_{}_{}_{}.tf_record".format(
                mode,
                list(filter(None, args["model_name_or_path"].split("/"))).pop(),
                str(args["max_seq_length"])
            ),
        )

    if os.path.exists(cached_features_file) and not args["overwrite_cache"]:
        logging.info("Loading features from cached file %s", cached_features_file)
        dataset, size = load_cache(cached_features_file, args["max_seq_length"])
    else:
        logging.info("Creating new cached dataset file at %s", cached_features_file)
        examples = utils.read_examples_from_file(args["data_dir"], mode, args["perturber"], args["level"])
        features = utils.convert_examples_to_features(
            examples,
            labels,
            args["max_seq_length"],
            tokenizer,
            cls_token_at_end=bool(args["model_type"] in ["xlnet"]),  # xlnet has a cls token at the end
            cls_token=tokenizer.cls_token,
            cls_token_segment_id=2 if args["model_type"] in ["xlnet"] else 0,
            sep_token=tokenizer.sep_token,
            sep_token_extra=bool(args["model_type"] in ["roberta"]),
            # roberta uses an extra separator b/w pairs of sentences,
            # cf. github.com/pytorch/fairseq/commit/1684e166e3da03f5b600dbb7855cb98ddfcd0805
            pad_on_left=bool(args["model_type"] in ["xlnet"]),  # pad on the left for xlnet
            pad_token=tokenizer.convert_tokens_to_ids([tokenizer.pad_token])[0],
            pad_token_segment_id=4 if args["model_type"] in ["xlnet"] else 0,
            pad_token_label_id=pad_token_label_id
        )
        logging.info("Saving features into cached file %s", cached_features_file)
        save_cache(features, cached_features_file)
        dataset, size = load_cache(cached_features_file, args["max_seq_length"])

    if mode == "train":
        dataset = dataset.repeat()
        dataset = dataset.shuffle(buffer_size=8192, seed=args["seed"])

    dataset = dataset.batch(batch_size, drop_remainder)
    dataset = dataset.prefetch(buffer_size=batch_size)

    return dataset, size


def main(argv):
    """

    :param argv:
    :return:
    """
    logging.set_verbosity(logging.INFO)
    args = flags.FLAGS.flag_values_dict()

    if (
            os.path.exists(args["output_dir"])
            and os.listdir(args["output_dir"])
            and args["do_train"]
            and not args["overwrite_output_dir"]
    ):
        raise ValueError(
            "Output directory ({}) already exists and is not empty. Use --overwrite_output_dir to overcome.".format(
                args["output_dir"]
            )
        )

    if args["perturber"] and not args["level"]:
        raise ValueError("Perturber is specified but level is not.")

    if args["level"] and not args["perturber"]:
        raise ValueError("Level is specified but perturber is not.")

    if args["fp16"]:
        tf.config.optimizer.set_experimental_options({"auto_mixed_precision": True})

    if args["tpu"]:
        resolver = tf.distribute.cluster_resolver.TPUClusterResolver(tpu=args["tpu"])
        tf.config.experimental_connect_to_cluster(resolver)
        tf.tpu.experimental.initialize_tpu_system(resolver)
        strategy = tf.distribute.experimental.TPUStrategy(resolver)
        args["n_device"] = args["num_tpu_cores"]
    elif len(args["gpus"].split(",")) > 1:
        args["n_device"] = len([f"/gpu:{gpu}" for gpu in args["gpus"].split(",")])
        strategy = tf.distribute.MirroredStrategy(devices=[f"/gpu:{gpu}" for gpu in args["gpus"].split(",")])
    elif args["no_cuda"]:
        args["n_device"] = 1
        strategy = tf.distribute.OneDeviceStrategy(device="/cpu:0")
    else:
        args["n_device"] = len(args["gpus"].split(","))
        strategy = tf.distribute.OneDeviceStrategy(device="/gpu:" + args["gpus"].split(",")[0])

    logging.warning(
        "n_device: %s, distributed training: %s, 16-bits training: %s",
        args["n_device"],
        bool(args["n_device"] > 1),
        args["fp16"],
    )

    labels = utils.get_labels(args["labels"])
    num_labels = len(labels)
    pad_token_label_id = 0
    config_class, model_class, tokenizer_class = MODEL_CLASSES[args["model_type"]]
    config = config_class.from_pretrained(
        args["config_name"] if args["config_name"] else args["model_name_or_path"],
        num_labels=num_labels,
        cache_dir=args["cache_dir"] if args["cache_dir"] else None
    )

    logging.info("Training/evaluation parameters %s", args)

    # Training
    if args["do_train"]:
        tokenizer = tokenizer_class.from_pretrained(
            args["tokenizer_name"] if args["tokenizer_name"] else args["model_name_or_path"],
            do_lower_case=args["do_lower_case"],
            cache_dir=args["cache_dir"] if args["cache_dir"] else None,
        )

        with strategy.scope():
            model = model_class.from_pretrained(
                args["model_name_or_path"],
                from_pt=bool(".bin" in args["model_name_or_path"]),
                #num_labels=num_labels,
                config=config,
                cache_dir=args["cache_dir"] if args["cache_dir"] else None,
            )
            # model.layers[-1].activation = tf.keras.activations.softmax

        train_batch_size = args["per_device_train_batch_size"] * args["n_device"]
        train_dataset, num_train_examples = load_and_cache_examples(
            args, tokenizer, labels, pad_token_label_id, train_batch_size, mode="train"
        )

        eval_batch_size = 8
        dev_dataset, num_dev_examples = load_and_cache_examples(
            args, tokenizer, labels, pad_token_label_id, eval_batch_size, mode="dev"
        )

        opt = tf.keras.optimizers.Adam(learning_rate=args["learning_rate"], epsilon=1e-05)
        if args["fp16"]:
            opt = tf.keras.mixed_precision.experimental.LossScaleOptimizer(opt, "dynamic")

        loss = tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True)
        metric = tf.keras.metrics.SparseCategoricalAccuracy("accuracy")

        model.compile(optimizer=opt, loss=loss, metrics=[metric])

        # train_dataset = strategy.experimental_distribute_dataset(train_dataset)

        history = model.fit(
            train_dataset,
            epochs=args["num_train_epochs"],
            steps_per_epoch=num_train_examples // train_batch_size,
            validation_data=dev_dataset,
            validation_steps=num_dev_examples // eval_batch_size,
            #  callbacks=[early_stopper]
        )

        # train(
        #     args,
        #     strategy,
        #     train_dataset,
        #     tokenizer,
        #     model,
        #     num_train_examples,
        #     labels,
        #     train_batch_size,
        #     pad_token_label_id
        # )

        if not os.path.exists(args["output_dir"]):
            os.makedirs(args["output_dir"])

        logging.info("Saving model to %s", args["output_dir"])

        model.save_pretrained(args["output_dir"])
        tokenizer.save_pretrained(args["output_dir"])

    # Evaluation
    if args["do_eval"]:
        tokenizer = tokenizer_class.from_pretrained(args["output_dir"], do_lower_case=args["do_lower_case"])
        checkpoints = []
        results = []

        if args["eval_all_checkpoints"]:
            checkpoints = list(
                os.path.dirname(c)
                for c in sorted(
                    glob.glob(args["output_dir"] + "/**/" + TF2_WEIGHTS_NAME, recursive=True),
                    key=lambda f: int("".join(filter(str.isdigit, f)) or -1)
                )
            )

        logging.info("Evaluate the following checkpoints: %s", checkpoints)

        if len(checkpoints) == 0:
            checkpoints.append(args["output_dir"])

        for checkpoint in checkpoints:
            global_step = checkpoint.split("-")[-1] if re.match(".*checkpoint-[0-9]", checkpoint) else "final"

            with strategy.scope():
                model = model_class.from_pretrained(checkpoint)

            y_true, y_pred, eval_loss = evaluate(
                args, strategy, model, tokenizer, labels, pad_token_label_id, mode="dev"
            )
            report = classification_report(y_true, y_pred)
            accuracy = metrics.accuracy_score(y_true, y_pred)

            if global_step:
                results.append({
                    global_step + "_report": report,
                    global_step + "_loss": eval_loss,
                    global_step + "_acc": accuracy
                })

        if args["perturber"] and args["level"]:
            output_eval_file = os.path.join(args["output_dir"], f"{args['perturber']}_{args['level']}_eval_results.txt")
        else:
            output_eval_file = os.path.join(args["output_dir"], "clean_eval_results.txt")

        with tf.io.gfile.GFile(output_eval_file, "w") as writer:
            for res in results:
                for key, val in res.items():
                    if "loss" in key or "acc" in key:
                        logging.info(key + " = " + str(val))
                        writer.write(key + " = " + str(val))
                        writer.write("\n")
                    else:
                        logging.info(key)
                        logging.info("\n" + report)
                        writer.write(key + "\n")
                        writer.write(report)
                        writer.write("\n")

    if args["do_predict"]:
        tokenizer = tokenizer_class.from_pretrained(args["output_dir"], do_lower_case=args["do_lower_case"])
        model = model_class.from_pretrained(args["output_dir"])
        eval_batch_size = args["per_device_eval_batch_size"] * args["n_device"]
        predict_dataset, _ = load_and_cache_examples(
            args, tokenizer, labels, pad_token_label_id, eval_batch_size, mode="test"
        )
        y_true, y_pred, pred_loss = evaluate(args, strategy, model, tokenizer, labels, pad_token_label_id, mode="test")

        if args["perturber"] and args["level"]:
            output_test_results_file = os.path.join(args["output_dir"],
                                                    f"{args['perturber']}_{args['level']}_test_results.txt")
            output_test_predictions_file = os.path.join(args["output_dir"],
                                                        f"{args['perturber']}_{args['level']}_test_predictions.txt")
        else:
            output_test_results_file = os.path.join(args["output_dir"], "clean_test_results.txt")
            output_test_predictions_file = os.path.join(args["output_dir"], "clean_test_predictions.txt")

        # report = metrics.classification_report(y_true, y_pred, digits=4)

        with tf.io.gfile.GFile(output_test_results_file, "w") as writer:
            report = classification_report(y_true, y_pred, digits=4)
            accuracy = metrics.accuracy_score(y_true, y_pred)

            logging.info("\n" + report)

            writer.write(report)
            writer.write("\n\nloss = " + str(pred_loss))
            logging.info("pred_loss: " + str(pred_loss))
            writer.write("\naccuracy = " + str(accuracy))
            logging.info("pred_acc:" + str(accuracy))

        with tf.io.gfile.GFile(output_test_predictions_file, "w") as writer:
            if args["perturber"] and args["level"]:
                test_file_name = f"test_{args['perturber']}_{args['level']}.txt"
            else:
                test_file_name = "test.txt"
            with tf.io.gfile.GFile(os.path.join(args["data_dir"], test_file_name), "r") as f:
                example_id = 0

                next(f)
                writer.write("\t".join(["gold_label", "pred_label", "sentence1", "sentence2"]) + "\n")
                for line in f:
                    splits = line.split("\t")
                    gold_label = splits[0]
                    if gold_label not in ["neutral", "entailment", "contradiction"]:
                        continue
                    sentence1 = splits[1]
                    sentence2 = splits[2]
                    if example_id >= len(y_pred):
                        break
                    assert gold_label == labels[y_true[example_id]], f"gold_label {gold_label} and y_true {y_true[example_id]} do not match!"
                    output_line = "\t".join([gold_label, labels[y_pred[example_id]], sentence1, sentence2.strip()]) + "\n"
                    writer.write(output_line)
                    example_id += 1
    return 0


if __name__ == '__main__':
    flags.mark_flag_as_required("data_dir")
    flags.mark_flag_as_required("output_dir")
    flags.mark_flag_as_required("model_name_or_path")
    flags.mark_flag_as_required("model_type")
    app.run(main)
