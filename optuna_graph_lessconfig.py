# system imports
import pickle
import numpy as np
from warnings import warn
import random
import json
from datetime import datetime
import time
import os
from pprint import pprint
import tqdm
from coolmom_pytorch import SGD
# pytorch imports
import torch
from torch.nn import L1Loss
import torch.optim as optim
from torch_geometric.data import Data
from torch_geometric.nn import TopKPooling, SAGPooling
import optuna

# Custom imports
from helpers import mol2graph
from helpers.EarlyStopping import EarlyStopping
from helpers.scale import normalize
from GraphPoolingNets import TopKPoolingNet, GraphConvPoolNet
from LinearNet import LinearNet
from torch_geometric.nn.conv import GraphConv, GATConv

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
if device == "cpu":
    warn("You are using CPU instead of CUDA. The computation will be longer...")


# Data parameters
DATASET_TYPE = "medium"
DATA_DIR = f"ala_dipep_{DATASET_TYPE}"
TARGET_FILE = f"free-energy-{DATASET_TYPE}.dat"
N_SAMPLES = 3815 if DATASET_TYPE == "small" else 21881 if DATASET_TYPE == "medium" else 50000 if DATASET_TYPE == "old" else 64074 if DATASET_TYPE == "big" else 48952
NORMALIZE_DATA = True
NORMALIZE_TARGET = True
OVERWRITE_PICKLES = False
UNSEEN_REGION = None  # can be "left", "right" or None. When is "left" we train on "right" and predict on "left"

if not OVERWRITE_PICKLES:
    warn("You are using EXISTING pickles, change this setting if you add features to nodes/edges ")

# Parameters
run_parameters = {
    "sin_cos": True,
    "graph_type": "De Bruijn",
    "out_channels": 4,
    "convolution": "GraphConv",
    "convolutions": 3,
    "learning_rate": 0.0001 if NORMALIZE_TARGET else 0.001,
    "epochs": 100,
    "patience": 10,
    "normalize_target": NORMALIZE_TARGET,
    "dataset_perc": 1,
    "shuffle": False,
    "train_split": 0.1,
    "validation_split": 0.1,
    "unseen_region": UNSEEN_REGION
}


def read_dataset(train_perc):
    run_parameters["train_split"] = train_perc
    if UNSEEN_REGION is not None:
        seen_region = 'right' if UNSEEN_REGION == 'left' else 'left'
        warn(f"Training on {seen_region} minima only. Testing on {UNSEEN_REGION} minima.")

        with open(f"{DATA_DIR}/left.json", "r") as l:
            left = json.load(l)

        with open(f"{DATA_DIR}/right.json", "r") as r:
            right = json.load(r)

        # Training on everything else
        if UNSEEN_REGION == "left":
            indexes = left
        else:
            indexes = right

        train_ind = [i for i in range(N_SAMPLES) if i not in indexes]
        # half to validation, half to test
        random.shuffle(indexes)
        split = np.int(0.5 * len(indexes))
        validation_ind = indexes[:split]
        test_ind = indexes[split:]
    else:
        indexes = [i for i in range(N_SAMPLES)]
        random.shuffle(indexes)
        indexes = indexes[:np.int(run_parameters["dataset_perc"] * N_SAMPLES)]
        split = np.int(run_parameters["train_split"] * len(indexes))
        train_ind = indexes[:split]
        split_2 = split + np.int(run_parameters["validation_split"] * len(indexes))
        validation_ind = indexes[split:split_2]
        test_ind = indexes[split_2:]

    graph_samples = []
    for i in range(N_SAMPLES):
        try:
            if OVERWRITE_PICKLES:
                raise FileNotFoundError

            with open("{}/{}-dihedrals-graph.pickle".format(DATA_DIR, i), "rb") as p:
                debruijn = pickle.load(p)

        except FileNotFoundError:
            atoms, edges, angles, dihedrals = mol2graph.get_richgraph("{}/{}.json".format(DATA_DIR, i))

            debruijn = mol2graph.get_central_overlap_graph(atoms, angles, dihedrals, shuffle=run_parameters["shuffle"],
                                                           sin_cos_decomposition=run_parameters["sin_cos"])

            if OVERWRITE_PICKLES:
                with open("{}/{}-dihedrals-graph.pickle".format(DATA_DIR, i), "wb") as p:
                    pickle.dump(debruijn, p)

        graph_samples.append(debruijn)

    with open(TARGET_FILE, "r") as t:
        target = torch.as_tensor([torch.tensor([float(v)]) for v in t.readlines()][:N_SAMPLES])
        if not NORMALIZE_TARGET:
            target = target.reshape(shape=(len(target), 1))

    # Compute STD and MEAN only on training data
    target_mean, target_std = 0, 1
    if NORMALIZE_TARGET:
        # training_target = torch.tensor([target[i] for i in train_ind])
        target_std = torch.std(target, dim=0)
        target_mean = torch.mean(target, dim=0)
        target = ((target - target_mean) / target_std).reshape(shape=(len(target), 1))

    if NORMALIZE_DATA:
        # Single graph normalization
        samples = normalize(graph_samples, train_ind, False)
    else:
        samples = graph_samples

    dataset = []
    for i, sample in enumerate(samples):
        dataset.append(
            Data(x=sample[0], edge_index=sample[1], y=target[i]).to(device)
        )

    return dataset, train_ind, validation_ind, test_ind, target_mean, target_std


def define_model(sample):
    pooling_layers = [1]
    pooling_type = "EdgePooling"
    convolution_type = "GraphConv"
    pooling_nodes_ratio = 0.5
    final_pooling = ["max_pool_x", "avg_pool_x", "sort_pooling", "topk"]
    dense_output = False
    channels_optuna = 1
    optuna_multiplier = 1
    final_nodes = 3
    models, hyperparams = [], []
    for pooling_layers_ in pooling_layers:
        for final_pooling_ in final_pooling:
            hyperparam = {
                "channels_optuna": channels_optuna,
                "dense_output": dense_output,
                "final_pooling": final_pooling_,
                "topk_ratio": pooling_nodes_ratio,
                "pooling_layers": pooling_layers_,
                "pooling_type": pooling_type,
                "final_nodes": final_nodes,
                "optuna_multiplier": optuna_multiplier
            }
            hyperparams.append(hyperparam)
            models.append(
                TopKPoolingNet(
                    sample, pooling_layers_, pooling_type,
                    pooling_nodes_ratio, convolution_type,
                    final_pooling_, dense_output, channels_optuna,
                    final_nodes, optuna_multiplier))
    return zip(models, hyperparams)


def define_linear_model(sample):
    nodes1 = 65
    nodes2 = 340
    nodes3 = 441
    nodes4 = 220
    models, hyperparams = [], []
    for layer in [1, 4]:
        hp = {
            "nodes1": nodes1,
            "nodes2": nodes2,
            "nodes3": nodes3,
            "nodes4": nodes4,
            "layers": layer,
        }
        pprint(hp)
        models.append(
            LinearNet(sample, nodes1, nodes2, nodes3, nodes4, layer)
        )
        hyperparams.append(hp)
    return zip(models, hyperparams)


def objective():
    seed = 13000
    random.seed(seed)
    torch.manual_seed(seed)
    train_perc = 0.1
    dataset, train_ind, validation_ind, test_ind, target_mean, target_std = read_dataset(train_perc)
    for model, hyperparameters in define_linear_model(dataset[0]):
        seed += 1
        model = model.to(device)
        stopping = EarlyStopping(run_parameters["patience"])
        pprint(hyperparameters)
        print(model)
        criterion = L1Loss()
        lr = 0.001#0.0003
        betta=(1-0.99)**(1/(run_parameters["epochs"]*2187))
        print("betta", betta) 
#        optimizer = optim.SGD(model.parameters(), lr=lr, momentum=0.84, weight_decay=0.0001)

        optimizer = SGD(model.parameters(), lr=lr, momentum=0.99,  weight_decay=0.0001, beta=betta)


        for i in range(run_parameters["epochs"]):
            model.train()
            random.shuffle(train_ind)
            train_losses = []
            for number, j in enumerate(tqdm.tqdm(train_ind)):
                # Forward pass: Compute predicted y by passing x to the model
                y_pred = model(dataset[j].to(device))
                
                # Compute and print loss
                loss = criterion(y_pred, dataset[j].y)
                # Zero gradients, perform a backward pass, and update the weights.
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                train_losses.append(loss.item())

            train_loss = torch.mean(torch.as_tensor(train_losses)).item()
            if NORMALIZE_TARGET:
                train_loss = train_loss * target_std
            # Compute validation loss
            model.eval()
            val_losses = []
            # save some memory
            with torch.no_grad():
                for j in validation_ind:
                    y_pred = model(dataset[j].to(device))
                    val_loss = criterion(y_pred, dataset[j].y)
                    val_losses.append(val_loss.item())

                val_loss = torch.mean(torch.as_tensor(val_losses)).item()
                if NORMALIZE_TARGET:
                    val_loss = val_loss * target_std
                print("Epoch {} - Validation MAE: {:.2f} - Train MAE: {:.2f}".format(i + 1, val_loss, train_loss))

                # Check Early Stopping
                if stopping.check(val_loss):
                    run_parameters["epochs_run"] = i + 1
                    print(f"Training finished because of early stopping. Best loss on validation: {stopping.best_score:.2f}")
                    break

        predictions, errors = [], []
        model.eval()
        for j in test_ind:
            # Forward pass: Compute predicted y by passing x to the model
            prediction = model(dataset[j].to(device))
            error = prediction - dataset[j].y
            predictions.append(prediction.item())
            errors.append(error.item())

        # Compute MAE
        mae = np.absolute(np.asarray(errors)).mean()
        if NORMALIZE_TARGET:
            mae *= target_std
        print("Mean Absolute Error on test: {:.2f}".format(mae))

        # Save predictions as json
        if "pooling_layers" in hyperparameters:
            directory = f"logs/{DATASET_TYPE}-{datetime.now().strftime('%m%d-%H%M')}-mae:{mae:.2f}-{hyperparameters['pooling_layers']}&{hyperparameters['final_pooling']}"
        else:
            directory = f"logs/{DATASET_TYPE}-{datetime.now().strftime('%m%d-%H%M')}-mae:{mae:.2f}-{hyperparameters['layers']}"

        os.makedirs(directory)
        with open(f"{directory}/result.json", "w") as f:
            json.dump({
                "hyperparameters": hyperparameters,
                "run_parameters": run_parameters,
                "predicted": predictions,
                "target": [float(dataset[i].y.item()) for i in test_ind],
                "target_std": float(target_std),
                "target_mean": float(target_mean),
                "test_frames": test_ind,
                "train_frames": train_ind,
            }, f)

        torch.save({
            "parameters": model.state_dict()
        }, f"{directory}/parameters.pt")


objective()


