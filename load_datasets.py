"""
Datasets
"""
import os
import tensorflow as tf

from absl import app
from absl import flags

from datasets import datasets, inversions
from datasets.tfrecord import tfrecord_filename

FLAGS = flags.FLAGS

flags.DEFINE_integer("train_batch", 128, "Batch size for training")
flags.DEFINE_integer("eval_batch", 4096, "Batch size for evaluation")
flags.DEFINE_integer("shuffle_buffer", 60000, "Dataset shuffle buffer size")
flags.DEFINE_integer("prefetch_buffer", 1, "Dataset prefetch buffer size (0 = autotune)")
flags.DEFINE_boolean("tune_num_parallel_calls", False, "Autotune num_parallel_calls")
flags.DEFINE_integer("eval_shuffle_seed", 0, "Evaluation shuffle seed for repeatability")
flags.DEFINE_integer("eval_max_examples", 0, "Max number of examples to evaluate for validation (default 0, i.e. all)")
flags.DEFINE_integer("trim_time_steps", 0, "For testing RNN vs. CNN handling varying time series length, allow triming to set size (default 0, i.e. use all data)")
flags.DEFINE_integer("feature_subset", 0, "For testing RNN vs. CNN handling varying numbers of features, allow only using a subset of the features (default 0, i.e. all the features")


class Dataset:
    """ Load datasets from tfrecord files """
    def __init__(self, num_classes, class_labels, num_domains,
            train_filenames, test_filenames, invert_name=None,
            train_batch=None, eval_batch=None,
            shuffle_buffer=None, prefetch_buffer=None,
            eval_shuffle_seed=None, eval_max_examples=None,
            tune_num_parallel_calls=None):
        """
        Initialize dataset

        Must specify num_classes and class_labels (the names of the classes).
        Other arguments if None are defaults from command line flags.

        For example:
            Dataset(num_classes=2, class_labels=["class1", "class2"])
        """
        # Sanity checks
        assert num_classes == len(class_labels), \
            "num_classes != len(class_labels)"

        # Set parameters
        self.invert_name = invert_name  # either None or source_name
        self.num_classes = num_classes
        self.class_labels = class_labels
        self.num_domains = num_domains
        self.train_batch = train_batch
        self.eval_batch = eval_batch
        self.shuffle_buffer = shuffle_buffer
        self.prefetch_buffer = prefetch_buffer
        self.eval_shuffle_seed = eval_shuffle_seed
        self.eval_max_examples = eval_max_examples
        self.tune_num_parallel_calls = tune_num_parallel_calls

        # Check inversions exist
        if invert_name is not None:
            assert invert_name in inversions.map_to_source, \
                "If invertible, must specify map_to_source() in datasets.inversions"
            assert invert_name in inversions.map_to_target, \
                "If invertible, must specify map_to_target() in datasets.inversions"

        # Set defaults if not specified
        if self.train_batch is None:
            self.train_batch = FLAGS.train_batch
        if self.eval_batch is None:
            self.eval_batch = FLAGS.eval_batch
        if self.shuffle_buffer is None:
            self.shuffle_buffer = FLAGS.shuffle_buffer
        if self.prefetch_buffer is None:
            self.prefetch_buffer = FLAGS.prefetch_buffer
        if self.eval_shuffle_seed is None:
            self.eval_shuffle_seed = FLAGS.eval_shuffle_seed
        if self.eval_max_examples is None:
            self.eval_max_examples = FLAGS.eval_max_examples
        if self.tune_num_parallel_calls is None:
            self.tune_num_parallel_calls = FLAGS.tune_num_parallel_calls

        # Load the dataset
        self.train, self.train_evaluation, self.test_evaluation = \
            self.load_dataset(train_filenames, test_filenames)

    def load_tfrecords(self, filenames, batch_size, count=False, evaluation=False):
        """
        Load data from .tfrecord files (requires less memory but more disk space)
        max_examples=0 -- no limit on the number of examples
        """
        if len(filenames) == 0:
            return None

        # Create a description of the features
        # See: https://www.tensorflow.org/tutorials/load_data/tf-records
        feature_description = {
            'x': tf.io.FixedLenFeature([], tf.string),
            'y': tf.io.FixedLenFeature([], tf.string),
        }

        def _parse_example_function(example_proto):
            """
            Parse the input tf.Example proto using the dictionary above.
            parse_single_example is without a batch, parse_example is with batches

            What's parsed returns byte strings, but really we want to get the
            tensors back that we encoded with tf.io.serialize_tensor() earlier,
            so also run tf.io.parse_tensor
            """
            parsed = tf.io.parse_single_example(serialized=example_proto,
                features=feature_description)

            x = tf.io.parse_tensor(parsed["x"], tf.float32)
            y = tf.io.parse_tensor(parsed["y"], tf.float32)

            # Trim to certain time series length (note single example, not batch)
            # shape before: [time_steps, features]
            # shape after:  [min(time_steps, trim_time_steps), features]
            if FLAGS.trim_time_steps != 0:
                x = tf.slice(x, [0, 0],
                    [tf.minimum(tf.shape(x)[0], FLAGS.trim_time_steps), tf.shape(x)[1]])

            # Trim to a certain number of features (the first feature_subset)
            if FLAGS.feature_subset != 0:
                x = tf.slice(x, [0, 0],
                    [tf.shape(x)[0], tf.minimum(tf.shape(x)[1], FLAGS.feature_subset)])

            return x, y

        # Interleave the tfrecord files
        files = tf.data.Dataset.from_tensor_slices(filenames)
        dataset = files.interleave(
            lambda x: tf.data.TFRecordDataset(x, compression_type='GZIP').prefetch(100),
            cycle_length=len(filenames), block_length=1)

        # TODO maybe use .cache() or .cache(filename)
        # See: https://www.tensorflow.org/beta/tutorials/load_data/images

        # If desired, take the first max_examples examples
        if evaluation and self.eval_max_examples != 0:
            dataset = dataset.take(self.eval_max_examples)

        if count:  # only count, so no need to shuffle
            pass
        elif evaluation:  # don't repeat since we want to evaluate entire set
            dataset = dataset.shuffle(self.shuffle_buffer, seed=self.eval_shuffle_seed)
        else:  # repeat and shuffle
            dataset = dataset.shuffle(self.shuffle_buffer).repeat()

        # Whether to do autotuning of prefetch or num_parallel_calls
        prefetch_buffer = self.prefetch_buffer
        num_parallel_calls = None
        if self.tune_num_parallel_calls:
            num_parallel_calls = tf.data.experimental.AUTOTUNE
        if self.prefetch_buffer == 0:
            prefetch_buffer = tf.data.experimental.AUTOTUNE

        dataset = dataset.map(_parse_example_function,
            num_parallel_calls=num_parallel_calls)
        dataset = dataset.batch(batch_size)
        dataset = dataset.prefetch(prefetch_buffer)

        return dataset

    def load_dataset(self, train_filenames, test_filenames):
        """
        Load the X dataset as a tf.data.Dataset from train/test tfrecord filenames
        """
        train_dataset = self.load_tfrecords(train_filenames, self.train_batch)
        eval_train_dataset = self.load_tfrecords(train_filenames,
            self.eval_batch, evaluation=True)
        eval_test_dataset = self.load_tfrecords(test_filenames,
            self.eval_batch, evaluation=True)

        return train_dataset, eval_train_dataset, eval_test_dataset

    def label_to_int(self, label_name):
        """ e.g. Bathe to 0 """
        return self.class_labels.index(label_name)

    def int_to_label(self, label_index):
        """ e.g. Bathe to 0 """
        return self.class_labels[label_index]


def load(dataset_name, num_domains, test=False, *args, **kwargs):
    """ Load a dataset (source and target). Names must be in datasets.names().

    If test=True, then load real test set. Otherwise, load validation set as
    the "test" data (for use during training and hyperparameter tuning).
    """
    # Sanity checks
    assert dataset_name in datasets.datasets, \
        dataset_name + " not a supported dataset, only "+str(datasets.datasets)

    # Get dataset information
    num_classes = datasets.datasets[dataset_name].num_classes
    class_labels = datasets.datasets[dataset_name].class_labels
    invert_name = dataset_name if datasets.datasets[dataset_name].invertible else None

    # Get dataset tfrecord filenames
    def _path(filename):
        """ Files are in datasets/ subdirectory. If the file exists, return it
        as an array since we may sometimes want more than one file for a
        dataset. If it doesn't exist, ignore it (some datasets don't have a test
        set for example)."""
        fn = os.path.join("datasets", filename)
        return [fn] if os.path.exists(fn) else []

    train_filenames = _path(tfrecord_filename(dataset_name, "train"))
    valid_filenames = _path(tfrecord_filename(dataset_name, "valid"))
    test_filenames = _path(tfrecord_filename(dataset_name, "test"))

    # By default use validation data as the "test" data, unless test=True
    if not test:
        test_filenames = valid_filenames
    # If test=True, then make "train" consist of both training and validation
    # data to match the original dataset.
    else:
        train_filenames += valid_filenames

    # Create all the train, test, evaluation, ... tf.data.Dataset objects within
    # a Dataset() class that stores them
    dataset = Dataset(num_classes, class_labels, num_domains,
        train_filenames, test_filenames, invert_name, *args, **kwargs)

    return dataset


def load_da(dataset, sources, target, *args, **kwargs):
    """
    Load the source(s) and target domains

    Input:
        dataset - one of the dataset names (e.g. ucihar)
        sources - comma-separated string of source domain numbers
        target - string of target domain number

    Returns:
        [source1_dataset, source2_dataset, ...], target_dataset
    """
    # Get proper dataset names
    sources = [dataset+"_"+x for x in sources.split(",")]

    if target is not None:
        target = dataset+"_"+target

    # Need to know how many domains for creating the proper-sized model, etc.
    num_domains = len(sources)

    if target is not None:
        num_domains += 1

    # Check they're all valid
    valid_names = names()

    for s in sources:
        assert s in valid_names, "unknown source domain: "+s

    assert target in valid_names, "unknown target domain: "+target

    # Load each source
    source_datasets = []

    for s in sources:
        source_datasets.append(load(s, num_domains, *args, **kwargs))

    # Load target
    if target is not None:
        target_dataset = load(target, num_domains, *args, **kwargs)

        # Check validity
        for i, s in enumerate(source_datasets):
            assert s.num_classes == target_dataset.num_classes, \
                "Adapting from source "+str(i)+" to target with different " \
                "classes not supported"
    else:
        target_dataset = None

    return source_datasets, target_dataset


def names():
    """ Returns list of all the available datasets to load """
    return datasets.names()


def main(argv):
    print("Available datasets:", names())

    # Example showing that the sizes and number of channels are matched
    sources, target = load_da("ucihar", "1,2", "3")

    print("Source 0:", sources[0].train)
    print("Target:", target.train)

    for i, source in enumerate(sources):
        assert source.train is not None, "dataset file probably doesn't exist"

        for x, y in source.train:
            print("Source "+str(i)+" x shape:", x.shape)
            print("Source "+str(i)+" y shape:", y.shape)
            break

    assert target.train is not None, "dataset file probably doesn't exist"

    for x, y in target.train:
        print("Target x shape:", x.shape)
        print("Target y shape:", y.shape)
        break


if __name__ == "__main__":
    app.run(main)
