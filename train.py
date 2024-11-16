# type: ignore
#
# Train a predictive model from horde payload inputs to predict inference time.
#
# Supports multi-processing, just run this multiple times and the processes will
# automatically work together on the training. We are training with torch and searching
# through network hyper parameters using Optuna.
#
# Requires two input files (both exactly the same format) which can be created by enabling
# the SAVE_KUDOS_TRAINING_DATA constant in the worker.
#   - inference-time-data.json
#   - inference-time-data-validation.json
#
# The output is a series of model checkpoints, "kudos_models/kudos-X-n.ckpt" Where n is the
# number of the trial and X is the study version. Once the best trial number is identified
# simply select the appropriate file.
#
# The stand-alone class in examples/kudos.py is the code to actually use the model.
#
# Requires also a local mysql database named "optuna" and assumes it can connect
# with user "root" password "root". Change to your needs.
#
# For visualisation with optuna dashboard:
#   optuna-dashboard mysql://root:root@localhost/optuna
#
# This is a quick hack to assist with kudos calculation.
import argparse
import json
import math
import os
import random
import time

import torch
import torch.nn as nn
from torch import optim
from torch.utils.data import DataLoader, Dataset

import hordelib

hordelib.initialise()
import pickle

import optuna

from hordelib.horde import HordeLib

random.seed()

# Database connection string for Optuna - don't use root :)
DB_CONNECTION_STRING = "mysql://root:root@localhost/optuna"

# # Where is our training data?
# TRAINING_DATA_FILENAME = "f:/ai/dev/AI-Horde-Worker/inference-time-data.json"
# VALIDATION_DATA_FILENAME = "f:/ai/dev/AI-Horde-Worker/inference-time-data-validation.json"

# Number of trials to run.
# Each trial generates a new neural network topology with new hyper parameters and trains it.
NUMBER_OF_STUDY_TRIALS = 2000

# The version number of our study. Bump for different model versions.
STUDY_VERSION = "v21"

# Hyper parameter search bounds
MIN_NUMBER_OF_EPOCHS = 50
MAX_NUMBER_OF_EPOCHS = 2000
MAX_HIDDEN_LAYERS = 6
MIN_NODES_IN_LAYER = 4
MAX_NODES_IN_LAYER = 128
MIN_LEARNING_RATE = 1e-5
MAX_LEARNING_RATE = 1e-1
MIN_WEIGHT_DECAY = 1e-6
MAX_WEIGHT_DECAY = 1e-1
MIN_DATA_BATCH_SIZE = 32
MAX_DATA_BATCH_SIZE = 512

# The study sampler to use
OPTUNA_SAMPLER = optuna.samplers.TPESampler(n_startup_trials=30, n_ei_candidates=30)
# OPTUNA_SAMPLER = optuna.samplers.NSGAIISampler()  # genetic algorithm

# We have the following inputs to our kudos calculation, for example:
PAYLOAD_EXAMPLE = {
    "sdk_api_job_info": {
        "id_": "7ba3b75b-6926-4e78-ad42-6763fa15c262",
        "ids": ["7ba3b75b-6926-4e78-ad42-6763fa15c262"],
        "payload": {
            "sampler_name": "k_euler",
            "cfg_scale": 24.0,
            "denoising_strength": None,
            "seed": "2066405361",
            "height": 1024,
            "width": 768,
            "seed_variation": None,
            "post_processing": [],
            "post_processing_order": "facefixers_first",
            "tiling": False,
            "hires_fix": False,
            "hires_fix_denoising_strength": None,
            "clip_skip": 1,
            "control_type": None,
            "image_is_control": False,
            "return_control_map": False,
            "facefixer_strength": None,
            "loras": None,
            "tis": None,
            "extra_texts": None,
            "workflow": None,
            "transparent": False,
            "use_nsfw_censor": False,
            "ddim_steps": 40,
            "n_iter": 1,
            "scheduler": "karras",
            "lora_count": 0,
            "ti_count": 0,
        },
        "model": "Dreamshaper",
        "source_processing": "img2img",
        "model_baseline": "stable_diffusion_1",
        "extra_source_images_count": 0,
        "extra_source_images_combined_size": 0,
        "source_image_size": 0,
        "source_mask_size": 0,
    },
    "state": "ok",
    "censored": False,
    "time_popped": 1729837827.8703332,
    "time_submitted": 1729837835.3562803,
    "time_to_generate": 4.450331687927246,
    "time_to_download_aux_models": None,
}
# And one output
# "time": 13.2032


KNOWN_POST_PROCESSORS = [
    "RealESRGAN_x4plus",
    "RealESRGAN_x2plus",
    "RealESRGAN_x4plus_anime_6B",
    "NMKD_Siax",
    "4x_AnimeSharp",
    "strip_background",
    "GFPGAN",
    "CodeFormers",
]
KNOWN_SCHEDULERS = [
    "simple",
    "karras",
]
KNOWN_SCHEDULERS.sort()
KNOWN_SAMPLERS = sorted(set(HordeLib.SAMPLERS_MAP.keys()))
KNOWN_CONTROL_TYPES = list(set(HordeLib.CONTROLNET_IMAGE_PREPROCESSOR_MAP.keys()))
KNOWN_CONTROL_TYPES.append("None")
KNOWN_CONTROL_TYPES.sort()
KNOWN_SOURCE_PROCESSING = HordeLib.SOURCE_IMAGE_PROCESSING_OPTIONS[:]
KNOWN_SOURCE_PROCESSING.append("txt2img")
KNOWN_SOURCE_PROCESSING.sort()
KNOWN_MODEL_BASELINES = [
    "stable_diffusion_1",
    "stable_diffusion_2",
    "stable_diffusion_xl",
    "stable_cascade",
    "flux_1",
]
KNOWN_MODEL_BASELINES.sort()


def parse_args():
    parser = argparse.ArgumentParser(description="ML Training Script with configurable parameters")

    # Training control
    parser.add_argument("--enable-training", action="store_true", default=False, help="Enable training mode")

    # Test mode
    parser.add_argument("--test-model", type=str, help="Path to model file for testing one by one")

    # Database configuration
    parser.add_argument(
        "--db-path",
        type=str,
        default="optuna_studies.db",
        help="Path to SQLite database file for Optuna",
    )

    # Data paths
    parser.add_argument(
        "--training-data",
        type=str,
        default="./inference-time-data.json",
        help="Path to training data file",
    )

    parser.add_argument(
        "--validation-data",
        type=str,
        default="./inference-time-data-validation.json",
        help="Path to validation data file",
    )

    # Study parameters
    parser.add_argument("--study-trials", type=int, default=2000, help="Number of trials to run")

    parser.add_argument("--study-version", type=str, default="v21", help="Version number of the study")

    return parser.parse_args()


# This is an example of how to use the final model, pass in a horde payload, get back a predicted time in seconds
def payload_to_time(model, payload):
    inputs = KudosDataset.payload_to_tensor(payload).squeeze()
    with torch.no_grad():
        output = model(inputs)
    return round(float(output.item()), 2)


# This is how to load the model required above
def load_model(model_filename):
    with open(model_filename, "rb") as infile:
        return pickle.load(infile)


# This is just an helper for walking through the validation dataset one line at a time
# and using the methods above to calculate an overall average percentage accuracy
def test_one_by_one(model_filename):
    dataset = []
    with open(VALIDATION_DATA_FILENAME) as infile:
        while line := infile.readline():
            dataset.append(json.loads(line))

    model = load_model(model_filename)

    perc = []
    total_job_time = 0
    total_time = 0
    for data in dataset:
        model_time = time.perf_counter()
        predicted = payload_to_time(model, data)
        total_time += time.perf_counter() - model_time
        actual = round(data["time"], 2)
        total_job_time += data["time"]

        diff = abs(actual - predicted)
        max_val = max(actual, predicted)
        percentage_accuracy = (1 - diff / max_val) * 100

        perc.append(percentage_accuracy)
        # Print the data if very inaccurate prediction
        if percentage_accuracy < 60:
            print(data)
        print(f"{predicted} predicted, {actual} actual ({round(percentage_accuracy, 1)}%)")

    avg_perc = round(sum(perc) / len(perc), 1)
    print(f"Average kudos calculation time {round((total_time*1000000)/len(perc))} micro-seconds")
    print(f"Average actual job time in the dataset {round(total_job_time/len(perc), 2)} seconds")
    print(f"Average accuracy = {avg_perc}%")


class KudosDataset(Dataset):
    def __init__(self, filename):
        self.data = []
        self.labels = []

        with open(filename) as infile:
            payload_list = json.load(infile)

            for payload in payload_list:
                if payload["time_to_generate"] is None:
                    continue
                self.data.append(KudosDataset.payload_to_tensor(payload)[0])
                self.labels.append(payload["time_to_generate"])

        self.labels = torch.tensor(self.labels).float()
        self.mixed_data = torch.stack(self.data)

    @classmethod
    def payload_to_tensor(cls, payload):
        payload = payload["sdk_api_job_info"]
        p = payload["payload"]
        data = []
        data_samplers = []
        data_control_types = []
        data_source_processing_types = []
        data_model_baseline = []
        data_post_processors = []
        data_schedulers = []
        data.append(
            [
                p["height"] / 1024,
                p["width"] / 1024,
                p["ddim_steps"] / 100,
                p["cfg_scale"] / 30,
                p.get("denoising_strength", 1.0) if p.get("denoising_strength", 1.0) is not None else 1.0,
                float(p.get("clip_skip", 1.0)),
                p.get("control_strength", 1.0) if p.get("control_strength", 1.0) is not None else 1.0,
                p.get("facefixer_strength", 1.0) if p.get("facefixer_strength", 1.0) is not None else 1.0,
                float(p.get("lora_count", 0.0)),
                float(p.get("ti_count", 0.0)),
                float(p.get("extra_source_images_count", 0.0)),
                float(p.get("extra_source_images_combined_size", 0.0)),
                float(p.get("source_image_size", 0.0)),
                float(p.get("source_mask_size", 0.0)),
                1.0 if p.get("hires_fix", True) else 0.0,
                1.0 if p.get("hires_fix_denoising_strength", True) else 0.0,
                1.0 if p.get("image_is_control", True) else 0.0,
                1.0 if p.get("return_control_map", True) else 0.0,
                1.0 if p.get("transparent", True) else 0.0,
                1.0 if p.get("source_image", True) else 0.0,
                1.0 if p.get("source_mask", True) else 0.0,
                1.0 if p.get("tiling", True) else 0.0,
                1.0 if p.get("post_processing_order", "facefixers_first") == "facefixers_first" else 0.0,
            ],
        )
        data_model_baseline.append(
            payload["model_baseline"] if payload["model_baseline"] in KNOWN_MODEL_BASELINES else "stable_diffusion_xl",
        )
        data_schedulers.append(p["scheduler"])
        data_samplers.append(p["sampler_name"] if p["sampler_name"] in KNOWN_SAMPLERS else "k_euler")
        data_control_types.append(
            p.get("control_type", "None") if p.get("control_type", "None") is not None else "None",
        )
        data_source_processing_types.append(payload.get("source_processing", "txt2img"))
        data_post_processors = p.get("post_processing", [])[:]
        _data_floats = torch.tensor(data).float()
        _data_model_baselines = cls.one_hot_encode(data_model_baseline, KNOWN_MODEL_BASELINES)
        _data_samplers = cls.one_hot_encode(data_samplers, KNOWN_SAMPLERS)
        _data_schedulers = cls.one_hot_encode(data_schedulers, KNOWN_SCHEDULERS)
        _data_control_types = cls.one_hot_encode(data_control_types, KNOWN_CONTROL_TYPES)
        _data_source_processing_types = cls.one_hot_encode(data_source_processing_types, KNOWN_SOURCE_PROCESSING)
        _data_post_processors = cls.one_hot_encode_combined(data_post_processors, KNOWN_POST_PROCESSORS)
        return torch.cat(
            (
                _data_floats,
                _data_model_baselines,
                _data_samplers,
                _data_schedulers,
                _data_control_types,
                _data_source_processing_types,
                _data_post_processors,
            ),
            dim=1,
        )

    @classmethod
    def one_hot_encode(cls, strings, unique_strings):
        one_hot = torch.zeros(len(strings), len(unique_strings))
        for i, string in enumerate(strings):
            one_hot[i, unique_strings.index(string)] = 1
        return one_hot

    @classmethod
    def one_hot_encode_combined(cls, strings, unique_strings):
        one_hot = torch.zeros(len(strings), len(unique_strings))
        for i, string in enumerate(strings):
            one_hot[i, unique_strings.index(string)] = 1

        return torch.sum(one_hot, dim=0, keepdim=True)

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        return self.mixed_data[idx], self.labels[idx]


def create_sequential_model(trial, layer_sizes, input_size, output_size=1):
    # Define the layer sizes
    layer_sizes = [input_size] + layer_sizes + [output_size]

    # Create the layers and activation functions
    layers = []
    for i in range(len(layer_sizes) - 1):
        layers.append(nn.Linear(layer_sizes[i], layer_sizes[i + 1]))
        if i < len(layer_sizes) - 2:
            layers.append(nn.ReLU())  # Use ReLU activation for all layers except the last one
            # Add a dropout layer
            if i > 0:
                drop = trial.suggest_float(f"dropout_l{i}", 0.05, 0.2, log=True)
                layers.append(nn.Dropout(drop))

    # Create the nn.Sequential model
    return nn.Sequential(*layers)


def objective(trial):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    trial.set_user_attr("name", "predict_kudos")

    # Network topology
    input_size = len(KudosDataset.payload_to_tensor(PAYLOAD_EXAMPLE)[0])
    num_hidden_layers = trial.suggest_int("hidden_layers", 1, MAX_HIDDEN_LAYERS, log=True)
    layers = []
    for i in range(num_hidden_layers):
        layers.append(
            trial.suggest_int(f"hidden_layer_{i}_size", MIN_NODES_IN_LAYER, MAX_NODES_IN_LAYER, log=True),
        )
    output_size = 1  # we want just the predicted time in seconds

    # Create the network
    model = create_sequential_model(trial, layers, input_size, output_size).to(device)

    # Optimiser
    optimizer_name = trial.suggest_categorical("optimizer", ["Adam", "RMSprop", "SGD"])
    lr = trial.suggest_float("lr", MIN_LEARNING_RATE, MAX_LEARNING_RATE, log=True)
    weight_decay = trial.suggest_float("weight_decay", MIN_WEIGHT_DECAY, MAX_WEIGHT_DECAY, log=True)

    optimizer = None

    if optimizer_name == "Adam":
        optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)
    elif optimizer_name == "RMSprop":
        optimizer = optim.RMSprop(model.parameters(), lr=lr, weight_decay=weight_decay)
    elif optimizer_name == "SGD":
        optimizer = optim.SGD(model.parameters(), lr=lr, weight_decay=weight_decay)

    if optimizer is None:
        raise Exception("Unknown optimizer")

    # Load training dataset
    train_dataset = KudosDataset(TRAINING_DATA_FILENAME)
    batch_start = int(math.ceil(math.log2(MIN_DATA_BATCH_SIZE)))
    batch_end = int(math.floor(math.log2(MAX_DATA_BATCH_SIZE)))
    batch_sizes = [2**i for i in range(batch_start, batch_end + 1)]
    batch = trial.suggest_categorical("batch_size", batch_sizes)
    train_loader = DataLoader(train_dataset, batch_size=batch, shuffle=True)

    # Load the validation dataset
    validate_dataset = KudosDataset(VALIDATION_DATA_FILENAME)
    validate_loader = DataLoader(validate_dataset, batch_size=64, shuffle=True)

    # Loss function
    criterion = nn.MSELoss()

    num_epochs = trial.suggest_int("num_epochs", MIN_NUMBER_OF_EPOCHS, MAX_NUMBER_OF_EPOCHS)
    total_loss = None
    for _ in range(num_epochs):
        # Train the model
        model.train()
        for data, labels in train_loader:
            data = data.to(device)
            labels = labels.to(device)
            optimizer.zero_grad()
            labels = labels.unsqueeze(1)
            outputs = model(data)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()

        model.eval()
        total_loss = 0
        with torch.no_grad():
            for data, labels in validate_loader:
                data = data.to(device)
                labels = labels.to(device)
                outputs = model(data)
                labels = labels.unsqueeze(1)
                loss = criterion(outputs, labels)
                total_loss += loss

        total_loss /= len(validate_loader)
        total_loss = round(float(total_loss), 2)

    # Pickle it as we'll forget the model architecture
    filename = f"kudos_models/kudos-{STUDY_VERSION}-{trial.number}.ckpt"
    with open(filename, "wb") as outfile:
        pickle.dump(model.to("cpu"), outfile)

    return total_loss


def main():

    if args.test_model:
        test_one_by_one(args.test_model)
        return

    if not ENABLE_TRAINING:
        return

    # Make our model output dir
    os.makedirs("kudos_models", exist_ok=True)

    if ENABLE_TRAINING:
        import optuna

        # Create the database directory if it doesn't exist
        db_dir = os.path.dirname(os.path.abspath(args.db_path))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        study = optuna.create_study(
            direction="minimize",
            study_name=f"kudos_model_{STUDY_VERSION}",
            storage=DB_CONNECTION_STRING,
            load_if_exists=True,
            sampler=OPTUNA_SAMPLER,
        )
        study.optimize(objective, n_trials=NUMBER_OF_STUDY_TRIALS)

        # Print the best hyperparameters
        print("Best trial:")
        trial = study.best_trial
        print("Value: ", trial.value)
        print("Params: ")
        for key, value in trial.params.items():
            print(f"{key}: {value}")

        # Calculate the accuracy of the best model
        best_filename = f"kudos_models/kudos-{STUDY_VERSION}-{trial.number}.ckpt"
        # model = test_one_by_one(best_filename)
        print(f"Best model file is: {best_filename}")


if __name__ == "__main__":
    # Parse command line arguments
    args = parse_args()

    # Set random seed
    random.seed()

    # Global constants now derived from args
    ENABLE_TRAINING = args.enable_training
    TRAINING_DATA_FILENAME = args.training_data
    VALIDATION_DATA_FILENAME = args.validation_data
    NUMBER_OF_STUDY_TRIALS = args.study_trials
    STUDY_VERSION = args.study_version

    # Create SQLite connection string
    DB_CONNECTION_STRING = f"sqlite:///{args.db_path}"
    main()
