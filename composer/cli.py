'''
The command-line interface for Composer.

'''

import re
import tqdm
import time
import json
import click
import logging
import datetime
import numpy as np
import composer.config
import composer.logging_utils as logging_utils
import composer.dataset.preprocess

from shutil import copy2
from pathlib import Path
from enum import Enum, unique
from composer.click_utils import EnumType
from composer.exceptions import DatasetError, InvalidParameterError
from composer.dataset.sequence import NoteSequence, EventSequence, OneHotEncodedEventSequence

def _set_verbosity_level(logger, value):
    '''
    Sets the verbosity level of the specified logger.

    '''

    x = getattr(logging, value.upper(), None)
    if x is None:
        raise click.BadParameter('Must be CRITICAL, ERROR, WARNING, INFO, or DEBUG, not \'{}\''.format(value))

    logger.setLevel(x)
        
@click.group()
@click.option('--verbosity', '-v', default='INFO', help='Either CRITICAL, ERROR, WARNING, INFO, or DEBUG.')
@click.option('--seed', type=int, help='Sets the seed of the random engine.')
@click.pass_context
def cli(ctx, verbosity, seed):
    '''
    A deep learning enabled music generator.

    '''

    if seed is None:
        # We use the current time as the seed rather than letting numpy seed
        # since we want to achieve consistent results across sessions.
        # Source: https://stackoverflow.com/a/45573061/7614083
        t = int(time.time() * 1000.0)
        seed = ((t & 0xff000000) >> 24) + ((t & 0x00ff0000) >> 8) + ((t & 0x0000ff00) <<  8) + ((t & 0x000000ff) << 24)

    logging_utils.init()
    _set_verbosity_level(logging.getLogger(), verbosity)

@cli.command()
@click.argument('dataset-path')
@click.argument('output-directory')
@click.option('--num-workers', '-w', default=16, help='The number of worker threads to spawn. Defaults to 16.')
@click.option('-c', '--config', 'config_filepath', default=None, 
              help='The path to the model configuration file. If unspecified, uses the default config for the model.')
@click.option('--transform/--no-transform', default=False, help='Indicates whether the dataset should be transformed. ' +
              'If true, a percentage of the dataset is duplicated and pitch shifted and/or time-stretched. Defaults to False.\n' +
              'Note: transforming a single sample produces three new samples: a pitch shifted one, time stretched one, and one with ' +
              'a combination of both. A transform percent value of 5%% means that the dataset will GROW by 3 times 5%% of the total size.')
@click.option('--transform-percent', default=0.50, help='The percentage of the dataset that should be transformed. Defaults to 50%% of the dataset.')
@click.option('--split/--no-split', default=True, help='Indicates whether the dataset should be split into train and test sets. Defaults to True.')
@click.option('--test-percent', default=0.30, help='The percentage of the dataset that is allocated to testing. Defaults to 30%%')
@click.option('--metadata/--no-metadata', 'output_metadata', default=True, help='Indicates whether to output metadata. Defaults to True.')
def preprocess(dataset_path, output_directory, num_workers, config_filepath,
               transform, transform_percent, split, test_percent, output_metadata):
    '''
    Preprocesses a raw dataset so that it can be used by the models.

    '''

    config = composer.config.get(config_filepath or get_default_config(model_type))
    output_directory = Path(output_directory)

    if split:
        composer.dataset.preprocess.split_dataset(config, dataset_path, output_directory, test_percent, 
                                                  transform, transform_percent, num_workers)
    else:
        composer.dataset.preprocess.convert_all(config, dataset_path, output_directory, num_workers)

    if not output_metadata: return
    with open(output_directory / 'metadata.json', 'w+') as metadata_file:
        # The metadata file is a dump of the settings used to preprocess the dataset.
        metadata = {
            'local_time': str(datetime.datetime.now()),
            'utc_time': str(datetime.datetime.utcnow()),
            'raw_dataset_path': str(Path(dataset_path).absolute()),
            'output_directory': str(output_directory.absolute()),
            'transform': transform,
            'transform_percent': transform_percent,
            'split': split,
            'test_percent': test_percent,
            'seed': int(np.random.get_state()[1][0])
        }

        json.dump(metadata, metadata_file, indent=True)
    
    # Copy the config file used to preprocess the dataset
    copy2(config.filepath, output_directory / 'config.yml')

def get_event_sequence_ranges(config):
    '''
    Gets the event sequence value ranges, dimensions, and ranges.

    :param config:
        A :class:`composer.config.ConfigInstance` containing the configuration values.
    :returns:
        The event value ranges, event dimensions, and event ranges.

    '''

    from composer.dataset.sequence import EventSequence
    event_value_ranges = EventSequence._compute_event_value_ranges(config.dataset.time_step_increment, \
                                        config.dataset.max_time_steps, config.dataset.velocity_bins)
    event_dimensions = EventSequence._compute_event_dimensions(event_value_ranges)
    event_ranges = EventSequence._compute_event_ranges(event_dimensions)

    return event_value_ranges, event_dimensions, event_ranges

def get_model_event_dimensions(config):
    '''
    Computes the dimension of a single event input in the network.

    :param config:
        A :class:`composer.config.ConfigInstance` containing the configuration values.
    :returns:
        The dimensions of an encoded event network input.

    '''
    
    from composer.dataset.sequence import OneHotEncodedEventSequence
    
    _, _, event_ranges = get_event_sequence_ranges(config)
    return OneHotEncodedEventSequence.get_one_hot_size(event_ranges)

def decode_to_event(config, encoded):
    '''
    Decodes an encoded event to a :class:`composer.dataset.sequence.Event`
    based on the configuration values.

    '''

    from composer.dataset.sequence import OneHotEncodedEventSequence

    event_value_ranges, event_dimensions, event_ranges = get_event_sequence_ranges(config)
    return OneHotEncodedEventSequence.one_hot_vector_as_event(encoded, event_ranges, event_value_ranges)

@unique
class ModelType(Enum):
    '''
    The type of the model.

    '''

    MUSIC_RNN = 'music_rnn'

    def create_model(self, config, **kwargs):
        '''
        Creates the model class associated with this :class:`ModelType` using the 
        values in the specified :class:`composer.config.ConfigInstance` object.

        :param config:
            A :class:`composer.config.ConfigInstance` containing the configuration values.
        :param **kwargs:
            External data passed to the creation method (i.e. data not in the configuration file)
        :returns:
            A :class:`tensorflow.keras.Model` object representing an instance of the specified model
            and the dimensions of an event (single feature and label) in the dataset.
        '''

        dimensions = get_model_event_dimensions(config)

        # Creates the MusicRNN model.
        def _create_music_rnn():
            from composer import models

            return models.MusicRNN(
                dimensions, config.model.window_size, config.model.lstm_layers_count,
                config.model.lstm_layer_sizes, config.model.lstm_dropout_probability,
                config.model.use_batch_normalization
            )

        # An easy way to map the creation functions to their respective types.
        # This is a lot better than doing something like an if/elif statement.
        function_map = {
            ModelType.MUSIC_RNN: _create_music_rnn
        }

        return function_map[self](), dimensions
        
    def get_dataset(self, dataset_path, mode, config, use_generator=False, max_files=None, show_progress_bar=True):
        '''
        Loads a dataset for this :class:`ModelType` using the values 
        in the specified :class:`composer.config.ConfigInstance` object.

        :param dataset_path:
            The path to the preprocessed dataset organized into two subdirectories: train and test.
        :param mode:
            A string indicating the dataset mode: ``train`` or ``test``.
        :param config:
            A :class:`composer.config.ConfigInstance` containing the configuration values.
        :param use_generator:
            Indicates whether the Dataset should be given as a generator object. Defaults to ``False``.
        :param max_files:
            The maximum number of files to load. Defaults to ``None`` which means that ALL
            files will be loaded.
        :param show_progress_bar:
            Indicates whether a loading progress bar should be displayed while the dataset is loaded
            into memory. Defaults to ``True``.
        :returns:
            A :class:`tensorflow.data.Dataset` object representing the dataset.
        
        '''

        from composer.models import load_dataset, EventEncodingType

        if mode not in ['train', 'test']:
            raise InvalidParameterError('\'{}\' is an invalid dataset mode! Must be one of: \'train\', \'test\'.'.format(mode))

        dataset_path = Path(dataset_path) / mode
        if not dataset_path.exists():
            raise DatasetError('Could not get {mode} dataset since the specified dataset directory, ' +
                               '\'{}\', has no {mode} folder.'.fromat(dataset_path, mode=mode))

        files = list(dataset_path.glob('**/*.{}'.format(composer.dataset.preprocess._OUTPUT_EXTENSION)))

        # Creates the MusicRNNDataset.
        def _load_music_rnn_dataset(files):
            if max_files is not None:
                files = files[:max_files]

            dataset, _ = load_dataset(files, config.train.batch_size, config.model.window_size, 
                                                input_event_encoding=EventEncodingType.ONE_HOT, 
                                                show_loading_progress_bar=show_progress_bar,
                                                use_generator=use_generator)

            return dataset

        # An easy way to map the creation functions to their respective types.
        # This is a lot better than doing something like an if/elif statement.
        function_map = {
            ModelType.MUSIC_RNN: _load_music_rnn_dataset
        }

        return function_map[self](files)

    def get_train_dataset(self, dataset_path, config, use_generator=False, max_files=None, show_progress_bar=True):
        '''
        Loads the training dataset for this :class:`ModelType` using the values 
        in the specified :class:`composer.config.ConfigInstance` object.

        :param dataset_path:
            The path to the preprocessed dataset organized into two subdirectories: train and test.
        :param config:
            A :class:`composer.config.ConfigInstance` containing the configuration values.
        :param use_generator:
            Indicates whether the Dataset should be given as a generator object. Defaults to ``False``.
        :param max_files:
            The maximum number of files to load. Defaults to ``None`` which means that ALL
            files will be loaded.
        :param show_progress_bar:
            Indicates whether a loading progress bar should be displayed while the dataset is loaded 
            into memory. Defaults to ``True``.
        :returns:
            A :class:`tensorflow.data.Dataset` object representing the training dataset.
        
        '''

        return self.get_dataset(dataset_path, 'train', config, use_generator, max_files, show_progress_bar)

    def get_test_dataset(self, dataset_path, config, use_generator=False, max_files=None, show_progress_bar=True):
        '''
        Loads the testing dataset for this :class:`ModelType` using the values 
        in the specified :class:`composer.config.ConfigInstance` object.

        :param dataset_path:
            The path to the preprocessed dataset organized into two subdirectories: train and test.
        :param config:
            A :class:`composer.config.ConfigInstance` containing the configuration values.
        :param use_generator:
            Indicates whether the Dataset should be given as a generator object. Defaults to ``False``.
        :param max_files:
            The maximum number of files to load. Defaults to ``None`` which means that ALL
            files will be loaded.
        :param show_progress_bar:
            Indicates whether a loading progress bar should be displayed while the dataset is loaded 
            into memory. Defaults to ``True``.
        :returns:
            A :class:`tensorflow.data.Dataset` object representing the testing dataset.
        
        '''

        return self.get_dataset(dataset_path, 'test', config, use_generator, max_files, show_progress_bar)

def get_default_config(model_type):
    '''
    Gets the default configuration filepath for the specified :class:`ModelType`.

    '''
    
    _FILEPATH_MAP = {
        ModelType.MUSIC_RNN: Path(__file__).parent / 'music_rnn_config.yml'
    }

    return _FILEPATH_MAP[model_type] 

def compile_model(model, config):
    '''
    Compiles the specified ``model``.

    '''

    from tensorflow.keras import optimizers, losses

    loss = losses.CategoricalCrossentropy(from_logits=True)
    optimizer = optimizers.Adam(learning_rate=config.train.learning_rate)
    model.compile(loss=loss, optimizer=optimizer, metrics=['accuracy'])

@cli.command()
@click.argument('model-type', type=EnumType(ModelType, False))
@click.option('-c', '--config', 'config_filepath', default=None, 
              help='The path to the model configuration file. If unspecified, uses the default config for the model.')
def summary(model_type, config_filepath):
    '''
    Prints a summary of the model.

    '''

    config = composer.config.get(config_filepath or get_default_config(model_type))

    model, dimensions = model_type.create_model(config)
    model.build(input_shape=(config.train.batch_size, config.model.window_size, dimensions))
    model.summary()

@cli.command()
@click.argument('model-type', type=EnumType(ModelType, False))
@click.argument('dataset-path')
@click.option('-c', '--config', 'config_filepath', default=None, 
              help='The path to the model configuration file. If unspecified, uses the default config for the model.')
@click.option('--steps', default=5, help='The number of steps to visualize. Defaults to 5.')
@click.option('--decode-events/-no-decode--events', default=True, help='Indicates whether the events should be decoded ' +
              'or displayed as their raw values (i.e. as a one-hot vector or integer id).')
def visualize_training(model_type, dataset_path, config_filepath, steps, decode_events):
    '''
    Visualize how the model will train. This displays the input and expected output (features and labels) for each step
    given the dataset.

    '''

    config = composer.config.get(config_filepath or get_default_config(model_type))
    dataset = model_type.get_train_dataset(dataset_path, config, max_files=1, show_progress_bar=False)

    count = 0
    events = []
    if model_type == ModelType.MUSIC_RNN:
        for batch_x, batch_y in dataset:
            features = batch_x.numpy().reshape(-1, batch_x.shape[-1])
            labels = batch_y.numpy().reshape(-1, batch_y.shape[-1])
            
            assert features.shape == labels.shape
            for i in range(len(features)):
                if count == steps: break
                count += 1
                
                x, y = features[i], labels[i]
                if decode_events:
                    x = decode_to_event(config, x)
                    y = decode_to_event(config, y)

                events.append((x, y))
    
    input_header = 'Input sequence: '
    input_sequence = ', '. join(str(x) for x, _ in events) 
    output_header = 'Output sequence: '
    output_sequence = ', '. join(str(y) for _, y in events)

    divider_length = max(len(input_header) + len(input_sequence),  len(output_header) + len(output_sequence))
    print('‾' * divider_length)

    header_colourization = logging_utils.colourize_string('%s', logging_utils.colorama.Fore.GREEN)
    print('{}{}'.format(header_colourization % input_header, input_sequence))
    print('_' * divider_length)
    print('‾' * divider_length)
    print('{}{}'.format(header_colourization % output_header, output_sequence))

    print('_' * divider_length)
    
    for index, (x, y) in enumerate(events):
        print('Step {}'.format(index + 1))
        print(' - input:             {}'.format(x))
        print(' - expected output:   {}'.format(y))

@cli.command()
@click.argument('model-type', type=EnumType(ModelType, False))
@click.argument('dataset-path')
@click.option('--logdir', default='./output/logdir/', help='The root log directory. Defaults to \'./output/logdir\'.')
@click.option('--restoredir', default=None, type=str, help='The directory of the model to continue training.')
@click.option('-c', '--config', 'config_filepath', default=None, 
              help='The path to the model configuration file. If unspecified, uses the default config for the model.')
@click.option('-e', '--epochs', 'epochs', default=10, help='The number of epochs to train for. Defaults to 10.')
@click.option('--use-generator/--no-use-generator', default=False,
              help='Indicates whether the dataset should be loaded in chunks during processing ' +
              '(rather than into memory all at once). Defaults to False.')
@click.option('--backup-config/--no-backup--config', default=True, help='Makes a copy of the configuration file used ' +
              'in the model\'s output directory. Defaults to True.')
@click.option('--max-files', default=None, help='The maximum number of files to load. Defaults to None, which means  ' + 
              'that ALL files will be loaded.', type=int)
@click.option('--save-freq', default=1000, help='The frequency at which to save the model. This can be \'epoch\' or integer. ' +
              'When using \'epoch\', the model will be saved every epoch; otherwise, it saves after the specified number of batches. ' +
              'Defaults to \'epoch\'. To set the epoch save period, use the \'epoch-save-period\' option.', type=str)
@click.option('--epoch-save-period', default=1, help='This value indicates the frequency, in epochs, that the model is saved. ' +
              'For example, if set to 10, the model will be saved every 10 epochs. Defaults to 1.', type=int)
def train(model_type, dataset_path, logdir, restoredir, config_filepath, epochs, 
          use_generator, backup_config, max_files, save_freq, epoch_save_period):
    '''
    Trains the specified model.

    '''

    import tensorflow as tf
    from tensorflow.keras.callbacks import TensorBoard, ModelCheckpoint

    config = composer.config.get(config_filepath or get_default_config(model_type))
    model, _ = model_type.create_model(config)
    compile_model(model, config)


    if restoredir is not None:
        checkpoint = tf.train.latest_checkpoint(restoredir)
        if checkpoint is None:
            logging.warn('Failed to load model checkpoint from \'{}\'.'.format(restoredir))
            exit(1)

        model.load_weights(checkpoint)
        model_logdir = Path(restoredir)

        initial_epoch = int(re.search(r'(?<=model-)(.*)(?=-)', str(checkpoint)).group(0))
    else:
        initial_epoch = 0
        model_logdir = Path(logdir) / '{}-{}'.format(model_type.name.lower(), datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S'))
        if backup_config:
            model_logdir.mkdir(parents=True, exist_ok=True)
            copy2(config.filepath, model_logdir / 'config.yml')

    tensorboard_callback = TensorBoard(log_dir=str(model_logdir.absolute()), update_freq=25, profile_batch=0, write_graph=False, write_images=False)
    model_checkpoint_path = model_logdir / 'model-{epoch:02d}-{loss:.2f}'

    is_epoch_save_freq = not save_freq.isdigit()
    model_checkpoint_callback = ModelCheckpoint(filepath=str(model_checkpoint_path.absolute()), monitor='loss', verbose=1, 
                                                save_freq='epoch' if is_epoch_save_freq else int(save_freq),
                                                period=epoch_save_period if is_epoch_save_freq else None, 
                                                save_best_only=False, mode='auto', save_weights_only=True,)

    train_dataset = model_type.get_train_dataset(dataset_path, config, use_generator, max_files=max_files)
    training_history = model.fit(train_dataset, epochs=epochs + initial_epoch, initial_epoch=initial_epoch,
                                 callbacks=[tensorboard_callback, model_checkpoint_callback])

@cli.command()
@click.argument('model-type', type=EnumType(ModelType, False))
@click.argument('dataset-path')
@click.argument('restoredir')
@click.option('-c', '--config', 'config_filepath', default=None, 
              help='The path to the model configuration file. If unspecified, uses the default config for the model.')
@click.option('--use-generator/--no-use-generator', default=False,
              help='Indicates whether the dataset should be loaded in chunks during processing ' +
              '(rather than into memory all at once). Defaults to False.')
@click.option('--max-files', default=None, help='The maximum number of files to load. Defaults to None, which means  ' + 
              'that ALL files will be loaded.', type=int)
def evaluate(model_type, dataset_path, restoredir, config_filepath, use_generator, max_files):
    '''
    Evaluate the specified model.

    '''

    import tensorflow as tf

    config = composer.config.get(config_filepath or get_default_config(model_type))  
    model, dimensions = model_type.create_model(config, dimensions)

    compile_model(model, config)
    model.load_weights(tf.train.latest_checkpoint(restoredir))
    model.build(input_shape=(config.train.batch_size, config.model.window_size, dimensions))

    test_dataset = model_type.get_test_dataset(dataset_path, config, use_generator, max_files=max_files)
    loss, accuracy = model.evaluate(test_dataset, verbose=0)
    logging.info('- Finished evaluating model. Loss: {:.4f}, Accuracy: {:.4f}'.format(loss, accuracy))

@cli.command()
@click.argument('model-type', type=EnumType(ModelType, False))
@click.argument('restoredir')
@click.argument('output-filepath')
@click.option('-c', '--config', 'config_filepath', default=None, 
              help='The path to the model configuration file. If unspecified, uses the default config for the model.')
@click.option('--prompt', '-p', 'prompt', default=None, help='The path of the MIDI file to prompt the network with. ' +
              'Defaults to None, meaning a random prompt will be created.')
@click.option('--prompt-length', default=10, help='Number of events to take from the start of the prompt. Defaults to 10.')
@click.option('--length', '-l', 'generate_length', default=1024, help='The length of the generated event sequence. Defaults to 1024')
@click.option('--temperature', default=1.0, help='Dictates how random the result is. Low temperature yields more predictable output. ' +
              'On the other hand, high temperature yields very random ("surprising") outputs. Defaults to 1.0.')
def generate(model_type, restoredir, output_filepath, config_filepath, prompt, prompt_length, generate_length, temperature):
    '''
    Generate a MIDI file.

    '''

    import tensorflow as tf

    config = composer.config.get(config_filepath or get_default_config(model_type))
    model, dimensions = model_type.create_model(config)
    
    compile_model(model, config)
    model.load_weights(tf.train.latest_checkpoint(restoredir))
    model.build(input_shape=(1, prompt_length, dimensions))

    if prompt is None:
        raise NotImplementedError()

    event_sequence = NoteSequence.from_midi(prompt).to_event_sequence(config.dataset.time_step_increment, \
                config.dataset.max_time_steps, config.dataset.velocity_bins)

    event_sequence.events = event_sequence.events[:prompt_length]

    def _encode(event):
        return OneHotEncodedEventSequence.event_as_one_hot_vector(event, event_sequence.event_ranges, \
                    event_sequence.event_value_ranges, as_numpy_array=True, numpy_dtype=np.float)

    def _decode(vector):
        return OneHotEncodedEventSequence.one_hot_vector_as_event(vector, event_sequence.event_ranges, \
                    event_sequence.event_value_ranges)

    x = [_encode(event) for event in event_sequence.events]
    x = tf.expand_dims(x, 0)
    
    vector_size = OneHotEncodedEventSequence.get_one_hot_size(event_sequence.event_ranges)
    model.reset_states()
    for i in tqdm.tqdm(range(generate_length)):
        predictions = model(x)
        predictions = tf.squeeze(predictions, 0)
        predictions = predictions / temperature

        predicted_id = tf.random.categorical(predictions, num_samples=1)[-1, 0].numpy()

        predicted_vector = np.zeros(vector_size)
        predicted_vector[predicted_id] = 1

        x = tf.expand_dims([predicted_vector], 0)
        event_sequence.events.append(_decode(predicted_vector))

    output_filepath = Path(output_filepath)
    output_filepath.parent.mkdir(parents=True, exist_ok=True)
    event_sequence.to_note_sequence().to_midi(str(output_filepath))