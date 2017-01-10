try:
    import Queue
except ImportError:
    import queue as Queue
import os
import shutil
import sys
from threading import Thread
from time import sleep

import numpy as np
from numpy.random import RandomState
from dataset_loaders.data_augmentation import random_transform

import dataset_loaders
from utils_parallel_loader import classproperty, grouper, overlap_grouper


class ThreadedDataset(object):
    """
    Threaded dataset.

    This is an abstract class and should not be used as is. Each
    specific dataset class should implement its `get_names` and
    `load_sequence` functions to load the list of filenames to be
    loaded and define how to load the data from the dataset,
    respectively.

    Mandatory attributes
        * debug_shape: any reasonable shape that can be used for debug purposes
        * name: the name of the dataset
        * non_void_nclasses: the number of *non-void* classes
        * path: a local path for the dataset
        * sharedpath: the network path where the dataset can be copied from
        * _void_labels: a list of void labels. Empty if none

    Optional attributes
        * data_shape: the shape of the data, when constant. Else (3, None,
            None)
        * has_GT: False if no mask is provided
        * GTclasses: a list of classes labels. To be provided when the
            classes labels (including the void ones) are not consecutive
        * _void_labels: a *list* of labels that are void classes.
        * _cmap: a *dictionary* of the form `class_id: (R, G, B)`. `class_id`
            is the class id in the original data.
        * _mask_labels: a *dictionary* of form `class_id: label`. `class_id`
            is the class id in the original data.


    Optional arguments
        * seq_per_video: the *maximum* number of sequences per each
            video (a.k.a. prefix). If 0, all sequences will be used.
            Default: 0.
        * seq_length: the number of frames per sequence. If 0, 4D arrays
            will be returned (not a sequence), else 5D arrays will be
            returned. Default: 0.
        * overlap: the number of frames of overlap between the first
            frame of one sample and the first frame of the next. Note
            that a negative overlap will instead specify the number of
            frames that are *skipped* between the last frame of one
            sample and the first frame of the next.
        * split: percentage of the training set to be used for training.
            The remainder will be used for validation
        * val_test_split: percentage of the validation set to be used
            for validation. The remainder will be used for test

    Parallel loader will automatically map all non-void classes to be
    sequential starting from 0 and then map all void classes to the
    next class. E.g., suppose non_void_nclasses = 4 and _void_classes = [3, 5]
    the non-void classes will be mapped to 0, 1, 2, 3 and the void
    classes will be mapped to 4, as follows:
        0 --> 0
        1 --> 1
        2 --> 2
        3 --> 4
        4 --> 3
        5 --> 4

    Note also that in case the original labels are not sequential, it
    suffices to list all the original labels as a list in GTclasses for
    parallel_loader to map the non-void classes sequentially starting
    from 0 and all the void classes to the next class. E.g. suppose
    non_void_nclasses = 5, GTclasses = [0, 2, 5, 9, 11, 12, 99] and
    _void_labels = [2, 99], then this will be the mapping:
         0 --> 0
         2 --> 5
         5 --> 1
         9 --> 2
        11 --> 3
        12 --> 4
        99 --> 5
    """
    def __init__(self,
                 seq_per_video=0,   # if 0 all sequences (or frames, if 4D)
                 seq_length=0,      # if 0, return 4D
                 overlap=None,
                 batch_size=1,
                 queues_size=50,
                 get_one_hot=False,
                 get_01c=False,
                 use_threads=False,
                 nthreads=1,
                 shuffle_at_each_epoch=True,
                 infinite_iterator=True,
                 return_list=False,  # for keras, return X,Y only
                 data_augm_kwargs={},
                 remove_mean=False,  # dataset stats
                 divide_by_std=False,  # dataset stats
                 remove_per_img_mean=False,  # img stats
                 divide_by_per_img_std=False,  # img stats
                 rng=None,
                 wait_time=0.05,
                 **kwargs):

        if len(kwargs):
            print('Ignored arguments: {}'.format(kwargs.keys()))

        # Set default values for the data augmentation params if not specified
        default_data_augm_kwargs = {
            'crop_size': None,
            'rotation_range': 0,
            'width_shift_range': 0,
            'height_shift_range': 0,
            'shear_range': 0,
            'zoom_range': 0,
            'channel_shift_range': 0,
            'fill_mode': 'nearest',
            'cval': 0,
            'cvalMask': 0,
            'horizontal_flip': False,
            'vertical_flip': False,
            'rescale': None,
            'spline_warp': False,
            'warp_sigma': 0.1,
            'warp_grid_size': 3,
            'gamma': 0,
            'gain': 1}

        default_data_augm_kwargs.update(data_augm_kwargs)
        self.data_augm_kwargs = default_data_augm_kwargs
        del(default_data_augm_kwargs, data_augm_kwargs)

        # Put crop_size into canonical form [c1, 2]
        cs = self.data_augm_kwargs['crop_size']
        if cs is not None:
            # Convert to list
            if isinstance(cs, int):
                cs = [cs, cs]
            elif isinstance(cs, tuple):
                cs = list(cs)
            # set 0, 0 to None
            if cs == [0, 0]:
                cs = None
            self.data_augm_kwargs['crop_size'] = cs

        # Do not support multithread without shuffling
        if use_threads and nthreads > 1 and not shuffle_at_each_epoch:
            raise NotImplementedError('Multiple threads are not order '
                                      'preserving')

        # Check that the implementing class has all the mandatory attributes
        mandatory_attrs = ['name', 'non_void_nclasses', 'debug_shape',
                           '_void_labels', 'path', 'sharedpath']
        missing_attrs = [attr for attr in mandatory_attrs if not
                         hasattr(self, attr)]
        if missing_attrs != []:
            raise NameError('Mandatory argument(s) missing: {}'.format(
                missing_attrs))

        # If variable sized dataset --> either batch_size 1 or crop
        if (not hasattr(self, 'data_shape') and batch_size > 1 and
                not self.data_augm_kwargs['crop_size']):
            raise ValueError(
                '{} has no `data_shape` attribute, this means that the '
                'shape of the samples varies across the dataset. You '
                'must either set `batch_size = 1` or specify a '
                '`crop_size`'.format(self.name))

        if seq_length and overlap and overlap >= seq_length:
            raise ValueError('`overlap` should be smaller than `seq_length`')

        # Create the `datasets` dir if missing
        if not os.path.exists(os.path.join(dataset_loaders.__path__[0],
                                           'datasets')):
            print('The dataset path does not exist. Making a dir..')
            the_path = os.path.join(dataset_loaders.__path__[0], 'datasets')
            # Follow the symbolic link
            if os.path.islink(the_path):
                the_path = os.path.realpath(the_path)
            os.makedirs(the_path)

        # Copy the data to the local path if not existing
        if not os.path.exists(self.path):
            print('The local path {} does not exist. Copying '
                  'dataset...'.format(self.path))
            shutil.copytree(self.sharedpath, self.path)
            print('Done.')

        # Save parameters in object
        self.seq_per_video = seq_per_video
        self.return_sequence = seq_length != 0
        self.seq_length = seq_length if seq_length else 1
        self.overlap = overlap if overlap is not None else self.seq_length - 1
        self.batch_size = batch_size
        self.queues_size = queues_size
        self.get_one_hot = get_one_hot
        self.get_01c = get_01c
        self.use_threads = use_threads
        self.nthreads = nthreads
        self.shuffle_at_each_epoch = shuffle_at_each_epoch
        self.infinite_iterator = infinite_iterator
        self.return_list = return_list
        self.remove_mean = remove_mean
        self.divide_by_std = divide_by_std
        self.remove_per_img_mean = remove_per_img_mean
        self.divide_by_per_img_std = divide_by_per_img_std
        self.rng = rng if rng is not None else RandomState(0xbeef)
        self.wait_time = wait_time

        self.has_GT = getattr(self, 'has_GT', True)

        # ...01c
        data_shape = list(getattr(self.__class__, 'data_shape',
                                  (None, None, 3)))
        if self.data_augm_kwargs['crop_size']:
            data_shape[-3:-1] = self.data_augm_kwargs['crop_size']  # change 01
        if self.get_01c:
            self.data_shape = data_shape
        else:
            self.data_shape = [data_shape[i] for i in
                               [2] + range(2) + range(3, len(data_shape))]

        # Load a dict of names, per video/subset/prefix/...
        self.names_per_subset = self.get_names()

        # Fill the sequences/batches lists and initialize everything
        self._fill_names_sequences()
        if len(self.names_sequences) == 0:
            raise RuntimeError('The name list cannot be empty')
        self._fill_names_batches(shuffle_at_each_epoch)

        if self.use_threads:
            # Initialize the queues
            self.names_queue = Queue.Queue(maxsize=self.queues_size)
            self.data_queue = Queue.Queue(maxsize=self.queues_size)
            self._init_names_queue()  # Fill the names queue

            # Start the data fetcher threads
            self.sentinel = object()  # guaranteed unique reference
            self.data_fetchers = []
            for _ in range(self.nthreads):
                data_fetcher = Thread(
                    target=threaded_fetch,
                    args=(self.names_queue, self.data_queue, self.sentinel,
                          self.fetch_from_dataset))
                data_fetcher.setDaemon(True)  # Die when main dies
                data_fetcher.start()
                self.data_fetchers.append(data_fetcher)
            # Give time to the data fetcher to die, in case of errors
            # sleep(1)

    def get_names(self):
        """ Loads ALL the names, per video.

        Should return a *dictionary*, where each element of the
        dictionary is a list of filenames. The keys of the dictionary
        should be the prefixes, i.e., names of the subsets of the
        dataset. If the dataset has no subset, 'default' can be used as
        a key.
        """
        raise NotImplementedError

    def load_sequence(self, sequence):
        """ Loads a 4D sequence from the dataset.

        Should return a *dict* with at least these keys:
            * 'data': the images or frames of the sequence
            * 'labels': the labels of the sequence
            * 'subset': the subset/clip/category/.. the sequence belongs to
            * 'filenames': the filenames of each image/frame of the sequence
        """
        raise NotImplementedError

    def _fill_names_sequences(self):
        names_sequences = {}

        # Cycle over prefix/subset/video/category/...
        for prefix, names in self.names_per_subset.items():
            seq_length = self.seq_length

            # Repeat the first and last elements so that the first and last
            # sequences are filled with repeated elements up/from the
            # middle element.
            extended_names = ([names[0]] * (seq_length // 2) + names +
                              [names[-1]] * (seq_length // 2))
            # Fill sequences with multiple el with form
            # [(prefix, name1), (prefix, name2), ...]. The names here
            # have overlap = (seq_length - 1), i.e., "stride" = 1
            sequences = [el for el in overlap_grouper(
                extended_names, seq_length, prefix=prefix)]
            # Sequences of frames with the requested overlap
            sequences = sequences[::self.seq_length - self.overlap]

            names_sequences[prefix] = sequences
        self.names_sequences = names_sequences

    def _fill_names_batches(self, shuffle):
        '''Create the desired batches of sequences

        * Select the desired sequences according to the parameters
        * Set self.nsamples, self.nbatches and self.names_batches.
        * Set self.names_batches, an iterator over the batches of names.
        '''
        names_sequences = []
        for prefix, sequences in self.names_sequences.items():

            # Pick only a subset of sequences per each video
            if self.seq_per_video:
                # Pick `seq_per_video` random indices
                idx = np.random.permutation(range(len(sequences)))[
                    :self.seq_per_video]
                # Select only those sequences
                sequences = np.array(sequences)[idx]

            names_sequences.extend(sequences)

        # Shuffle the sequences
        if shuffle:
            self.rng.shuffle(names_sequences)

        # Group the sequences into minibatches
        names_batches = [el for el in grouper(names_sequences,
                                              self.batch_size)]
        self.nsamples = len(names_sequences)
        self.nbatches = len(names_batches)
        self.names_batches = iter(names_batches)

    def _init_names_queue(self):
        for _ in range(self.queues_size):
            try:
                name_batch = self.names_batches.next()
                self.names_queue.put(name_batch)
            except StopIteration:
                # Queue is bigger than the tot number of batches
                break

    def __iter__(self):
        return self

    def __next__(self):
        return self.next()

    def next(self):
        return self._step()

    def _step(self):
        '''Return one batch

        In case of threading, get one batch from the `data_queue`.
        The infinite loop allows to wait for the fetchers if data is
        consumed too fast.
        '''
        done = False
        while not done:
            if self.use_threads:
                # THREADS
                # Kill main process if fetcher died
                if all([not df.isAlive() for df in self.data_fetchers]):
                    import sys
                    print('All fetchers threads died. I will suicide!')
                    sys.exit(0)
                try:
                    # Get one minibatch from the out queue
                    data_batch = self.data_queue.get(False)
                    self.data_queue.task_done()
                    # Exception handling
                    if len(data_batch) == 3:
                        if isinstance(data_batch[1], IOError):
                            print('WARNING: Image corrupted or missing!')
                            print(data_batch[1])
                            continue  # fetch the next element
                        elif isinstance(data_batch[1], Exception):
                            raise data_batch[0], data_batch[1], data_batch[2]
                    done = True
                    # Refill the names queue, if we still have batches
                    try:
                        name_batch = self.names_batches.next()
                        self.names_queue.put(name_batch)
                    except StopIteration:
                        pass
                # The data_queue is empty: the epoch is over or we
                # consumed the data too fast
                except Queue.Empty:
                    if not self.names_queue.unfinished_tasks:
                        # END OF EPOCH - The data_queue is empty, i.e.
                        # the name_batches is empty: refill both
                        self._fill_names_batches(self.shuffle_at_each_epoch)
                        self._init_names_queue()  # Fill the names queue
                        if not self.infinite_iterator:
                            raise StopIteration
            else:
                # NO THREADS
                try:
                    name_batch = self.names_batches.next()
                    data_batch = self.fetch_from_dataset(name_batch)
                    done = True
                except StopIteration:
                    # END OF EPOCH - The name_batches is empty: refill it
                    self._fill_names_batches(self.shuffle_at_each_epoch)
                    if not self.infinite_iterator:
                        raise
                    # else, loop to the next image
                except IOError as e:
                    print('WARNING: Image corrupted or missing!')
                    print(e)

        assert(data_batch is not None)
        return data_batch

    def fetch_from_dataset(self, batch_to_load):
        """
        Return *batches* of 5D sequences/clips or 4D images.

        `batch_to_load` contains the indices of the first frame/image of
        each element of the batch.
        `load_sequence` should return a numpy array of 2 or more
        elements, the first of which 4-dimensional (frame, 0, 1, c)
        or (frame, c, 0, 1) containing the data and the second 3D or 4D
        containing the label.
        """
        batch_ret = {}

        # Create batches
        for el in batch_to_load:

            if el is None:
                continue

            # Load sequence, format is (s, 0, 1, c)
            ret = self.load_sequence(el)
            raw_data = ret['data'].copy()
            seq_x, seq_y = ret['data'], ret['labels']

            # Per-image normalization
            if self.remove_per_img_mean:
                seq_x -= seq_x.mean(axis=tuple(range(seq_x.ndim - 1)),
                                    keepdims=True)
            if self.divide_by_per_img_std:
                seq_x /= seq_x.std(axis=tuple(range(seq_x.ndim - 1)),
                                   keepdims=True)
            # Dataset statistics normalization
            if self.remove_mean:
                seq_x -= getattr(self, 'mean', 0)
            if self.divide_by_std:
                seq_x /= getattr(self, 'std', 1)

            # Make sure data is in 4D
            if seq_x.ndim == 3:
                seq_x = seq_x[np.newaxis, ...]
                raw_data = raw_data[np.newaxis, ...]
            assert seq_x.ndim == 4
            # and labels in 3D
            if self.has_GT:
                if seq_y.ndim == 2:
                    seq_y = seq_y[np.newaxis, ...]
                assert seq_y.ndim == 3

            # Perform data augmentation, if needed
            seq_y = seq_y[..., None]  # Add extra dim to simplify computation
            seq_x, seq_y = random_transform(
                seq_x, seq_y,
                nclasses=self.nclasses,
                void_label=self.void_labels,
                **self.data_augm_kwargs)
            seq_y = seq_y[..., 0]#.astype('int32')  # Undo extra dim

            if self.has_GT and self._void_labels != []:
                # Map all void classes to non_void_nclasses and shift the other
                # values accordingly, so that the valid values are between 0
                # and non_void_nclasses-1 and the void_classes are all equal to
                # non_void_nclasses.
                void_l = self._void_labels
                void_l.sort(reverse=True)
                mapping = self._get_mapping()

                # Apply the mapping
                seq_y[seq_y == self.non_void_nclasses] = -1
                for i in sorted(mapping.keys()):
                    if i == self.non_void_nclasses:
                        continue
                    seq_y[seq_y == i] = mapping[i]
                try:
                    seq_y[seq_y == -1] = mapping[self.non_void_nclasses]
                except KeyError:
                    # none of the original classes was self.non_void_nclasses
                    pass

            # Transform targets seq_y to one hot code if get_one_hot
            # is True
            if self.has_GT and self.get_one_hot:
                nc = (self.non_void_nclasses if self._void_labels == [] else
                      self.non_void_nclasses + 1)
                sh = seq_y.shape
                seq_y = seq_y.flatten()
                seq_y_hot = np.zeros((seq_y.shape[0], nc),
                                     dtype='int32')
                seq_y = seq_y.astype('int32')
                seq_y_hot[range(seq_y.shape[0]), seq_y] = 1
                seq_y_hot = seq_y_hot.reshape(sh + (nc,))
                seq_y = seq_y_hot

            # Dimshuffle if get_01c is False
            if not self.get_01c:
                # s,0,1,c --> s,c,0,1
                seq_x = seq_x.transpose([0, 3, 1, 2])
                if self.has_GT and self.get_one_hot:
                    seq_y = seq_y.transpose([0, 3, 1, 2])
                raw_data = raw_data.transpose([0, 3, 1, 2])

            # Return 4D images
            if not self.return_sequence:
                seq_x = seq_x[0, ...]
                if self.has_GT:
                    seq_y = seq_y[0, ...]
                raw_data = raw_data[0, ...]

            ret['data'], ret['labels'] = seq_x, seq_y
            ret['raw_data'] = raw_data
            # Append the data of this batch to the minibatch array
            for k, v in ret.iteritems():
                batch_ret.setdefault(k, []).append(v)

        for k, v in batch_ret.iteritems():
            try:
                batch_ret[k] = np.array(v)
            except ValueError:
                # Variable shape: cannot wrap with a numpy array
                pass
        if self.return_list:
            return [batch_ret['data'], batch_ret['labels']]
        else:
            return batch_ret

    def reset(self, shuffle, reload_sequences_from_dataset=True):
        '''Reset the dataset loader

        Resets the dataset loader according to the current parameters.
        If the parameters changed since initialization, by resetting the
        dataset they will be taken into account.

        Note that `reset` stops all the fetcher threads and makes sure the
        queues are emptied before adding new elements to them. This
        can introduce a slowdown in the fetching process. For this
        reason `shuffle` should be used to reshuffle the data when the
        parameters of the dataset have not been modified.
        '''
        if reload_sequences_from_dataset:
            # Get all the sequences of names from the dataset, with the
            # desired overlap
            self._fill_names_sequences()

        # Select the sequences we want, according to the parameters
        # Sets self.nsamples, self.nbatches and self.names_batches.
        # Self.names_batches is an iterator over the batches of names.
        self._fill_names_batches(shuffle)

        # Reset the queues
        if self.use_threads:
            # Empty names_queue
            with self.names_queue.mutex:
                done = False
                while not done:
                    try:
                        self.names_queue.get(False)
                        self.names_queue.task_done()
                    except Queue.Empty:
                        done = True
            # Wait for the fetchers to be done
            while not self.names_queue.unfinished_tasks:
                sleep(self.wait_time)
            # Empty the data_queue
            self.data_queue.queue.clear()
            self.data_queue.all_tasks_done.notify_all()
            self.data_queue.unfinished_tasks = 0

            # Refill the names queue
            self._init_names_queue()

    def shuffle(self):
        '''Shuffles the sequences and creates new batches, according to
        the initial parameters. To account for changes in the parameters
        of the dataset use `reset`.'''
        self._fill_names_batches(True)

    def finish(self):
        # Stop fetchers
        for _ in self.data_fetchers:
            self.names_queue.put(self.sentinel)
        while any([df.isAlive() for df in self.data_fetchers]):
            sleep(self.wait_time)
        # Kill threads
        for data_fetcher in self.data_fetchers:
            data_fetcher.join()

    def get_mean(self):
        return getattr(self, 'mean', [])

    def get_std(self):
        return getattr(self, 'std', [])

    @classproperty
    def nclasses(self):
        '''The number of classes in the output mask.'''
        return (self.non_void_nclasses + 1 if hasattr(self, '_void_labels') and
                self._void_labels != [] else self.non_void_nclasses)

    @classmethod
    def get_void_labels(self):
        '''Returns the void label(s)

        If the dataset has void labels, returns self.non_void_nclasses,
        i.e. the label to which all the void labels are mapped. Else,
        returns an empty list.'''
        return ([self.non_void_nclasses] if hasattr(self, '_void_labels') and
                self._void_labels != [] else [])

    @classproperty
    def void_labels(self):
        return self.get_void_labels()

    @classmethod
    def _get_mapping(self):
        if hasattr(self, 'GTclasses'):
            self.GTclasses.sort()
            mapping = {cl: i for i, cl in enumerate(
                set(self.GTclasses) - set(self._void_labels))}
            for l in self._void_labels:
                mapping[l] = self.non_void_nclasses
        else:
            mapping = {}
            delta = 0
            # Prepare the mapping
            for i in range(self.non_void_nclasses + len(self._void_labels)):
                if i in self._void_labels:
                    mapping[i] = self.non_void_nclasses
                    delta += 1
                else:
                    mapping[i] = i - delta
        return mapping

    @classmethod
    def _get_inv_mapping(self):
        mapping = self._get_mapping()
        return {v: k for k, v in mapping.items()}

    @classmethod
    def get_cmap(self):
        cmap = getattr(self, '_cmap', {})
        assert isinstance(cmap, dict)
        inv_mapping = self._get_inv_mapping()
        cmap = np.array([cmap[inv_mapping[k]] for k in
                         sorted(inv_mapping.keys())])
        if cmap.max() > 1:
            # assume labels are in [0, 255]
            cmap = cmap / 255.  # not inplace or rounded to int
        return cmap

    @classmethod
    def get_mask_labels(self):
        mask_labels = getattr(self, '_mask_labels', {})
        assert isinstance(mask_labels, dict)
        if mask_labels == {}:
            return []
        inv_mapping = self._get_inv_mapping()
        return np.array([mask_labels[inv_mapping[k]] for k in
                         sorted(inv_mapping.keys())])

    def get_cmap_values(self):
        return self._cmap.values()


def threaded_fetch(names_queue, data_queue, sentinel, fetch_from_dataset):
    """
    Fill the data_queue.

    Whenever there are names in the names queue, it will read them,
    fetch the corresponding data and fill the data_queue.

    Note that in case of errors, it will put the exception object in the
    data_queue.
    """
    while True:
        try:
            # Grabs names from queue
            batch_to_load = names_queue.get()

            if batch_to_load is sentinel:
                names_queue.task_done()
                break

            # Load the data
            minibatch_data = fetch_from_dataset(batch_to_load)

            # Place it in data_queue
            data_queue.put(minibatch_data)

            # Signal to the names queue that the job is done
            names_queue.task_done()
        except:
            # If any uncaught exception, pass it along and move on
            data_queue.put(sys.exc_info())
            names_queue.task_done()
