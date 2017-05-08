#!/usr/bin/env python3

import os
import gzip
import tarfile
import pickle
import struct

import re
from bs4 import BeautifulSoup

import pandas
import numpy
import scipy.sparse
import sklearn.preprocessing
import stemming.porter2 as stemming

from functools import reduce

from time import time
from auxiliary import (
    formatDuration,
    normaliseString,
    download
)

preprocess_suffix = "preprocessed"
original_suffix = "original"
preprocessed_extension = ".sparse.pkl.gz"

data_sets = {
    "mouse retina": {
        "tags": {
            "example": "cell",
            "feature": "gene",
            "value": "count"
        },
        "split": False,
        "preprocessing methods": None,
        "URLs": {
            "values": {
                "full": "ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE63nnn/GSE63472/suppl/GSE63472_P14Retina_merged_digital_expression.txt.gz"
            },
            "labels": {
                "full": "http://mccarrolllab.com/wp-content/uploads/2015/05/retina_clusteridentities.txt"
            }
        },
        "load function": lambda x: loadMouseRetinaDataSet(x)
    },
    
    "MNIST": {
        "tags": {
            "example": "digit",
            "feature": "pixel",
            "value": "pixel value"
        },
        "split": ["training", "test"],
        "preprocessing methods": None,
        "URLs": {
            "values": {
                    "training":
                        "http://yann.lecun.com/exdb/mnist/train-images-idx3-ubyte.gz",
                    "test":
                        "http://yann.lecun.com/exdb/mnist/t10k-images-idx3-ubyte.gz"
            },
            "labels": {
                    "training":
                        "http://yann.lecun.com/exdb/mnist/train-labels-idx1-ubyte.gz",
                    "test":
                        "http://yann.lecun.com/exdb/mnist/t10k-labels-idx1-ubyte.gz"
            },
        },
        "load function": lambda x: loadMNISTDataSet(x)
    },
    
    "MNIST (binarised)": {
        "tags": {
            "example": "digit",
            "feature": "pixel",
            "value": "pixel value"
        },
        "split": ["training", "validation", "test"],
        "preprocessing methods": ["binarise"],
        "URLs": {
            "values": {
                    "training":
                        "http://www.cs.toronto.edu/~larocheh/public/datasets/binarized_mnist/binarized_mnist_train.amat",
                    "validation":
                        "http://www.cs.toronto.edu/~larocheh/public/datasets/binarized_mnist/binarized_mnist_valid.amat",
                    "test":
                        "http://www.cs.toronto.edu/~larocheh/public/datasets/binarized_mnist/binarized_mnist_test.amat"
            },
            "labels": {
                    "training": None,
                    "validation": None,
                    "test": None
            },
        },
        "load function": lambda x: loadBinarisedMNISTDataSet(x)
    },
    
    "Reuters": {
        "tags": {
            "example": "document",
            "feature": "word",
            "value": "count"
        },
        "split": False,
        "preprocessing methods": None,
        "URLs": {
            "all": {
                "full": "http://www.daviddlewis.com/resources/testcollections/reuters21578/reuters21578.tar.gz"
            }
        },
        "load function": lambda x: loadReutersDataSet(x)
    },
    
    "20 Newsgroups": {
        "tags": {
            "example": "document",
            "feature": "word",
            "value": "count"
        },
        "split": ["training", "test"],
        "preprocessing methods": None,
        "URLs": {
            "all": {
                "full":
                    "http://qwone.com/~jason/20Newsgroups/20news-bydate.tar.gz"
            }
        },
        "load function": lambda x: load20NewsgroupsDataSet(x)
    },
    
    "blobs": {
        "split": False,
        "preprocessing methods": None,
        "URLs": {
            "all": {
                "full": "http://people.compute.dtu.dk/s147246/datasets/blobs.pkl.gz"
            }
        },
        "load function": lambda x: loadSampleDataSet(x)
    },
    
    "circles": {
        "split": False,
        "preprocessing methods": None,
        "URLs": {
            "all": {
                "full": "http://people.compute.dtu.dk/s147246/datasets/circles.pkl.gz"
            }
        },
        "load function": lambda x: loadSampleDataSet(x)
    },
    
    "moons": {
        "split": False,
        "preprocessing methods": None,
        "URLs": {
            "all": {
                "full": "http://people.compute.dtu.dk/s147246/datasets/moons.pkl.gz"
            }
        },
        "load function": lambda x: loadSampleDataSet(x)
    },
    
    "sample": {
        "split": False,
        "preprocessing methods": None,
        "URLs": {
            "all": {
                "full": "http://people.compute.dtu.dk/s152421/data-sets/count_samples.pkl.gz"
            }
        },
        "load function": lambda x: loadSampleDataSet(x)
    }
}

class DataSet(object):
    def __init__(self, name,
        values = None, preprocessed_values = None,
        labels = None, example_names = None, feature_names = None,
        feature_selection = None, feature_parameter = None,
        preprocessing_methods = [], preprocessed = None,
        kind = "full", version = "original",
        directory = "data"):
        
        super(DataSet, self).__init__()
        
        # Name of data set
        self.name = normaliseString(name)
        
        # Title (proper name) of data set
        self.title = dataSetTitle(self.name)
        
        # Tags (with names for examples, feature, and values) of data set
        self.tags = dataSetTags(self.title)
        
        # Values and their names as well as labels in data set
        self.values = None
        self.count_sum = None
        self.preprocessed_values = None
        self.labels = None
        self.example_names = None
        self.feature_names = None
        self.number_of_examples = None
        self.number_of_features = None
        self.update(values, preprocessed_values, labels, example_names,
            feature_names)
        
        # Feature selction and preprocessing methods
        self.feature_selection = feature_selection
        self.feature_parameter = feature_parameter
        self.preprocessing_methods = preprocessing_methods
        
        if preprocessed is None:
            data_set_preprocessing_methods = \
                dataSetPreprocessingMethods(self.title)
            if data_set_preprocessing_methods:
                self.preprocessed = True
            else:
                self.preprocessed = False
        else:
            self.preprocessed = preprocessed
        
        if self.preprocessed:
            self.preprocessing_methods = data_set_preprocessing_methods
        
        # Kind of data set (full, training, validation, test)
        self.kind = kind
        
        # Split indices for training, validation, and test sets
        self.split_indices = None
        
        # Version of data set (original, reconstructed)
        self.version = version
        
        # Directories for data set
        self.directory = os.path.join(directory, self.name)
        self.preprocess_directory = os.path.join(self.directory,
            preprocess_suffix)
        self.original_directory = os.path.join(self.directory,
            original_suffix)
        
        self.preprocessedPath = preprocessedPathFunction(
            self.preprocess_directory, self.name)
        
        if self.kind == "full":
            
            print("Data set:")
            print("    title:", self.title)
            if self.feature_selection:
                print("    feature selection:", self.feature_selection)
            else:
                print("    feature selection: none")
            if self.feature_parameter: 
                print("    feature parameter:", self.feature_parameter)
            else:
                print("    feature size: full")
            if not self.preprocessed and self.preprocessing_methods:
                print("    processing methods:")
                for preprocessing_method in self.preprocessing_methods:
                    print("        ", preprocessing_method)
            elif self.preprocessed:
                print("    processing methods: already done")
            else:
                print("    processing methods: none")
            print()
            
            if self.values is None:
                self.load()
            
            if self.preprocessed_values is None:
                self.preprocess()
    
    def update(self, values = None, preprocessed_values = None, labels = None,
        example_names = None, feature_names = None):
        
        if values is not None:
            
            self.values = values
            
            self.count_sum = self.values.sum(axis = 1).reshape(-1, 1)
            
            M_values, N_values = values.shape
            
            if example_names is not None:
                self.example_names = example_names
                M_examples = example_names.shape[0]
                assert M_values == M_examples
            
            if feature_names is not None:
                self.feature_names = feature_names
                N_features = feature_names.shape[0]
                assert N_values == N_features
            
            self.number_of_examples = M_values
            self.number_of_features = N_values
        
        else:
            
            if example_names is not None and feature_names is not None:
                
                self.example_names = example_names
                self.feature_names = feature_names
        
        if labels is not None:
            self.labels = labels
        
        if preprocessed_values is not None:
            self.preprocessed_values = preprocessed_values
    
    def load(self):
        
        sparse_path = self.preprocessedPath()
        
        if os.path.isfile(sparse_path):
            print("Loading data set from sparse representation.")
            data_dictionary = loadFromSparseData(sparse_path)
        else:
            original_paths = downloadDataSet(self.title, self.original_directory)
            
            print()
            
            data_dictionary = loadOriginalDataSet(self.title, original_paths)
            
            print()
            
            if not os.path.exists(self.preprocess_directory):
                os.makedirs(self.preprocess_directory)
            
            print("Saving data set in sparse representation.")
            saveAsSparseData(data_dictionary, sparse_path)
        
        self.update(
            values = data_dictionary["values"],
            labels = data_dictionary["labels"],
            example_names = data_dictionary["example names"],
            feature_names = data_dictionary["feature names"]
        )
        
        if "split indices" in data_dictionary:
            self.split_indices = data_dictionary["split indices"]
        
        print()
    
    def preprocess(self):
        
        if not self.preprocessing_methods and not self.feature_selection:
            self.update(preprocessed_values = self.values)
            return
        
        sparse_path = self.preprocessedPath(
            preprocessing_methods = self.preprocessing_methods,
            feature_selection = self.feature_selection, 
            feature_parameter = self.feature_parameter
        )
        
        if os.path.isfile(sparse_path):
            print("Loading preprocessed data from sparse representation.")
            data_dictionary = loadFromSparseData(sparse_path)
        
        else:
            
            if not self.preprocessed and self.preprocessing_methods:
                preprocessed_values = preprocessValues(self.values,
                    self.preprocessing_methods, self.preprocessedPath)
                
                print()
            
            else:
                
                preprocessed_values = self.values
            
            if self.feature_selection:
                values_dictionary, feature_names = selectFeatures(
                    {"original": self.values,
                     "preprocessed": preprocessed_values},
                    self.feature_names,
                    self.feature_selection,
                    self.feature_parameter,
                    self.preprocessedPath
                )
                
                values = values_dictionary["original"]
                preprocessed_values = values_dictionary["preprocessed"]
            
                print()
            
            else:
                values = self.values
                feature_names = self.feature_names
            
            data_dictionary = {
                "values": values,
                "preprocessed values": preprocessed_values,
                "feature names": feature_names
            }
            
            print("Saving preprocessed data set in sparse representation.")
            saveAsSparseData(data_dictionary, sparse_path)
        
        self.update(
            values = data_dictionary["values"],
            preprocessed_values = data_dictionary["preprocessed values"],
            feature_names = data_dictionary["feature names"]
        )
        
        print()
    
    def defaultSplittingMethod(self):
        if self.split_indices:
            return "indices"
        else:
            return "random"
    
    def split(self, method = "default", fraction = 0.9):
        
        if method == "default":
            method = self.defaultSplittingMethod()
        
        sparse_path = self.preprocessedPath(
            preprocessing_methods = self.preprocessing_methods,
            feature_selection = self.feature_selection,
            feature_parameter = self.feature_parameter,
            splitting_method = method,
            splitting_fraction = fraction
        )
        
        print("Splitting:")
        print("    method:", method)
        print("    fraction: {:.1f} %".format(100 * fraction))
        print()
        
        if os.path.isfile(sparse_path):
            print("Loading split data sets from sparse representation.")
            split_data_dictionary = loadFromSparseData(sparse_path)
        
        else:
            
            data_dictionary = {
                "values": self.values,
                "preprocessed values": self.preprocessed_values,
                "labels": self.labels,
                "example names": self.example_names,
                "split indices": self.split_indices
            }
            
            split_data_dictionary = splitDataSet(data_dictionary, method,
                fraction)
            
            print()
            
            print("Saving split data sets in sparse representation.")
            saveAsSparseData(split_data_dictionary, sparse_path)
        
        training_set = DataSet(
            name = self.name,
            values = split_data_dictionary["training set"]["values"],
            preprocessed_values = \
                split_data_dictionary["training set"]["preprocessed values"],
            labels = split_data_dictionary["training set"]["labels"],
            example_names = split_data_dictionary["training set"]["example names"],
            feature_names = self.feature_names,
            feature_selection = self.feature_selection,
            preprocessing_methods = self.preprocessing_methods,
            kind = "training"
        )
        
        validation_set = DataSet(
            name = self.name,
            values = split_data_dictionary["validation set"]["values"],
            preprocessed_values = \
                split_data_dictionary["validation set"]["preprocessed values"],
            labels = split_data_dictionary["validation set"]["labels"],
            example_names = split_data_dictionary["validation set"]["example names"],
            feature_names = self.feature_names,
            feature_selection = self.feature_selection,
            preprocessing_methods = self.preprocessing_methods,
            kind = "validation"
        )
        
        test_set = DataSet(
            name = self.name,
            values = split_data_dictionary["test set"]["values"],
            preprocessed_values = \
                split_data_dictionary["test set"]["preprocessed values"],
            labels = split_data_dictionary["test set"]["labels"],
            example_names = split_data_dictionary["test set"]["example names"],
            feature_names = self.feature_names,
            feature_selection = self.feature_selection,
            preprocessing_methods = self.preprocessing_methods,
            kind = "test"
        )
        
        print()
        
        print(
            "Data sets with {} features:\n".format(
                training_set.number_of_features) +
            "    Training sets: {} examples.\n".format(
                training_set.number_of_examples) +
            "    Validation sets: {} examples.\n".format(
                validation_set.number_of_examples) +
            "    Test sets: {} examples.".format(
                test_set.number_of_examples)
        )
        
        print()
        
        return training_set, validation_set, test_set

def dataSetTitle(name):
    
    title = None
        
    for data_set in data_sets:
        if normaliseString(data_set) == name:
            title = data_set
    
    if not title:
        raise KeyError("Data set not found.")
    
    return title

def dataSetTags(title):
    if "tags" in data_sets[title]:
        return data_sets[title]["tags"]
    else:
        tags = {
            "example": "example",
            "feature": "feature",
            "value": "value"
        }
        return tags

def dataSetPreprocessingMethods(title):
    return data_sets[title]["preprocessing methods"]

def downloadDataSet(title, directory):
    
    if not os.path.exists(directory):
        os.makedirs(directory)
    
    URLs = data_sets[title]["URLs"]
    
    paths = {}
    
    for values_or_labels in URLs:
        paths[values_or_labels] = {}
        
        for kind in URLs[values_or_labels]:
            
            URL = URLs[values_or_labels][kind]
            
            if not URL:
                continue
            
            URL_filename = os.path.split(URL)[-1]
            extension = os.extsep + URL_filename.split(os.extsep, 1)[-1]
            
            name = normaliseString(title)
            filename = name + "-" + values_or_labels + "-" + kind
            path = os.path.join(directory, filename) + extension
            
            paths[values_or_labels][kind] = path
            
            if not os.path.isfile(path):
                
                print("Downloading {} for {} set.".format(
                    values_or_labels, kind, title))
                start_time = time()
                
                download(URL, path)
                
                duration = time() - start_time
                print("Data set downloaded ({}).".format(formatDuration(duration)))
                
                print()
    
    return paths

def loadOriginalDataSet(title, paths):
    print("Loading original data set.")
    start_time = time()
    
    data_dictionary = data_sets[title]["load function"](paths)
    
    duration = time() - start_time
    print("Original data set loaded ({}).".format(formatDuration(duration)))
    
    return data_dictionary

def preprocessedPathFunction(preprocess_directory = "", name = ""):
    
    def preprocessedPath(base_name = None, preprocessing_methods = None,
        feature_selection = None, feature_parameter = None,
        splitting_method = None, splitting_fraction = None):
        
        base_path = os.path.join(preprocess_directory, name)
        
        filename_parts = [base_path]
        
        if base_name:
            filename_parts.append(normaliseString(base_name))
        
        if feature_selection:
            filename_parts.append(normaliseString(feature_selection))
            if feature_parameter:
                filename_parts.append(str(feature_parameter))
        
        if preprocessing_methods:
            filename_parts.extend(map(normaliseString, preprocessing_methods))
        
        if splitting_method:
            filename_parts.append("split")
            filename_parts.append(normaliseString(splitting_method))
            
            if splitting_fraction:
                filename_parts.append(str(splitting_fraction))
            
        path = "-".join(filename_parts) + preprocessed_extension
        
        return path
    
    return preprocessedPath

def selectFeatures(values_dictionary, feature_names, feature_selection = None, feature_parameter = None, preprocessPath = None):
    
    feature_selection = normaliseString(feature_selection)
    
    print("Selecting features.")
    start_time = time()
    
    if type(values_dictionary) == dict:
        values = values_dictionary["original"]
    
    M, N = values.shape
    
    if feature_selection == "remove_zeros":
        total_feature_sum = values.sum(axis = 0)
        indices = total_feature_sum != 0
    
    elif feature_selection == "keep_gini_indices_above":
        gini_indices = loadWeights(values, "gini", preprocessPath)
        if not feature_parameter:
            feature_parameter = 0.1
        indices = gini_indices > feature_parameter

    elif feature_selection == "keep_highest_gini_indices":
        gini_indices = loadWeights(values, "gini", preprocessPath)
        gini_sorted_indices = numpy.argsort(gini_indices)
        indices = numpy.sort(gini_sorted_indices[-feature_parameter:])
        
    elif feature_selection == "keep_variances_above":
        variances = values.var(axis = 0)
        if not feature_parameter:
            feature_parameter = 0.5
        indices = variances > feature_parameter

    elif feature_selection == "keep_highest_variances":
        variances = values.var(axis = 0)
        variance_sorted_indices = numpy.argsort(variances)
        indices = numpy.sort(variance_sorted_indices[-feature_parameter:])
        
    else:
        indices = slice(N)
    
    feature_selected_values = {}
    
    for version, values in values_dictionary.items():
        feature_selected_values[version] = values[:, indices]
    
    feature_selected_feature_names = feature_names[indices]
    
    duration = time() - start_time
    print("Features selected ({}).".format(formatDuration(duration)))
    
    return feature_selected_values, feature_selected_feature_names

def preprocessValues(values, preprocessing_methods = [], preprocessPath = None):
    
    print("Preprocessing values.")
    start_time = time()
    
    preprocesses = []
    
    for preprocessing_method in preprocessing_methods:
        
        if preprocessing_method in ["gini", "idf"]:
            weight_method = preprocessing_method
            preprocess = lambda x: applyWeights(x, weight_method,
                preprocessPath)
        
        elif preprocessing_method == "normalise":
            preprocess = normalise
        
        elif preprocessing_method == "binarise":
            preprocess = binarise
        
        else:
            preprocess = lambda x: x
        
        preprocesses.append(preprocess)
    
    preprocessed_values = reduce(lambda v, p: p(v), preprocesses, values)
    
    duration = time() - start_time
    print("Values preprocessed ({}).".format(formatDuration(duration)))
    
    return preprocessed_values

def splitDataSet(data_dictionary, method = "default", fraction = 0.9):
    
    print("Splitting data set.")
    start_time = time()
    
    if method == "default":
        if self.split_indices:
            method = "indices"
        else:
            method = "random"
    
    method = normaliseString(method)
    
    M = data_dictionary["values"].shape[0]
    
    numpy.random.seed(42)
    
    if method == "random":
        
        M_training_validation = int(fraction * M)
        M_training = int(fraction * M_training_validation)
        
        shuffled_indices = numpy.random.permutation(M)
        
        training_indices = shuffled_indices[:M_training]
        validation_indices = shuffled_indices[M_training:M_training_validation]
        test_indices = shuffled_indices[M_training_validation:]
    
    elif method == "indices":
        
        split_indices = data_dictionary["split indices"]
        
        training_indices = split_indices["training"]
        test_indices = split_indices["test"]
        
        if "validation" in split_indices:
            validation_indices = split_indices["validation"]
        else:
            M_training_validation = training_indices.stop
            
            M_training = int(fraction * M_training_validation)
            
            training_indices = slice(M_training)
            validation_indices = slice(M_training, M_training_validation)
    
    split_data_dictionary = {
        "training set": {
            "values": data_dictionary["values"][training_indices],
            "preprocessed values": None,
            "labels": None,
            "example names": data_dictionary["example names"][training_indices]
        },
        "validation set": {
            "values": data_dictionary["values"][validation_indices],
            "preprocessed values": None,
            "labels": None,
            "example names": data_dictionary["example names"][validation_indices]
        },
        "test set": {
            "values": data_dictionary["values"][test_indices],
            "preprocessed values": None,
            "labels": None,
            "example names": data_dictionary["example names"][test_indices]
        },
    }
    
    if "labels" in data_dictionary and data_dictionary["labels"] is not None:
        split_data_dictionary["training set"]["labels"] = \
            data_dictionary["labels"][training_indices]
        split_data_dictionary["validation set"]["labels"] = \
            data_dictionary["labels"][validation_indices]
        split_data_dictionary["test set"]["labels"] = \
            data_dictionary["labels"][test_indices]
    
    if "preprocessed values" in data_dictionary:
        split_data_dictionary["training set"]["preprocessed values"] = \
            data_dictionary["preprocessed values"][training_indices]
        split_data_dictionary["validation set"]["preprocessed values"] = \
            data_dictionary["preprocessed values"][validation_indices]
        split_data_dictionary["test set"]["preprocessed values"] = \
            data_dictionary["preprocessed values"][test_indices]
    
    duration = time() - start_time
    print("Data set split ({}).".format(formatDuration(duration)))
    
    return split_data_dictionary

def loadFromSparseData(path):
    
    start_time = time()
    
    def converter(data):
        if type(data) == scipy.sparse.csr.csr_matrix:
            return data.todense().A
        else:
            return data
    
    with gzip.open(path, "rb") as data_file:
        data_dictionary = pickle.load(data_file)
    
    for key in data_dictionary:
        if "set" in key:
            for key2 in data_dictionary[key]:
                data_dictionary[key][key2] = converter(data_dictionary[key][key2])
        else:
            data_dictionary[key] = converter(data_dictionary[key])
    
    duration = time() - start_time
    print("Data loaded from sparse representation" +
        " ({}).".format(formatDuration(duration)))
    
    return data_dictionary

def saveAsSparseData(data_dictionary, path):
    
    start_time = time()
    
    sparse_data_dictionary = {}
    
    def converter(data):
        if type(data) == numpy.ndarray and data.ndim == 2:
            return scipy.sparse.csr_matrix(data)
        else:
            return data
    
    for key in data_dictionary:
        if "set" in key:
            sparse_data_dictionary[key] = {}
            for key2 in data_dictionary[key]:
                sparse_data_dictionary[key][key2] = \
                    converter(data_dictionary[key][key2])
        else:
            sparse_data_dictionary[key] = converter(data_dictionary[key])
    
    with gzip.open(path, "wb") as data_file:
        pickle.dump(sparse_data_dictionary, data_file)
    
    duration = time() - start_time
    print("Data saved in sparse representation" +
        " ({}).".format(formatDuration(duration)))

def loadMouseRetinaDataSet(paths):
    
    values_data = pandas.read_csv(paths["values"]["full"], sep='\s+',
        index_col = 0, compression = "gzip", engine = "python"
    )
    
    values = values_data.values.T
    example_names = numpy.array(values_data.columns.tolist())
    feature_names = numpy.array(values_data.index.tolist())
    
    values = values.astype(float)
    
    labels = numpy.zeros(example_names.shape)
    
    with open(paths["labels"]["full"], "r") as labels_data:
        for line in labels_data.read().split("\n"):
            
            if line == "":
                continue
            
            example_name, label = line.split("\t")
            
            labels[example_names == example_name] = int(label)
    
    data_dictionary = {
        "values": values,
        "labels": labels,
        "example names": example_names,
        "feature names": feature_names
    }
    
    return data_dictionary

def loadMNISTDataSet(paths):
    
    values = {}
    
    for kind in paths["values"]:
        with gzip.open(paths["values"][kind], "rb") as values_stream:
            _, M, r, c = struct.unpack(">IIII", values_stream.read(16))
            values_buffer = values_stream.read(M * r * c)
            values_flat = numpy.frombuffer(values_buffer, dtype = numpy.uint8)
            values[kind] = values_flat.reshape(-1, r * c)
    
    N = r * c
    
    labels = {}
    
    for kind in paths["labels"]:
        with gzip.open(paths["labels"][kind], "rb") as labels_stream:
            _, M = struct.unpack(">II", labels_stream.read(8))
            labels_buffer = labels_stream.read(M)
            labels[kind] = numpy.frombuffer(labels_buffer, dtype = numpy.int8)
    
    M_training = values["training"].shape[0]
    M_test = values["test"].shape[0]
    M = M_training + M_test
    
    split_indices = {
        "training": slice(0, M_training),
        "test": slice(M_training, M)
    }
    
    values = numpy.concatenate((values["training"], values["test"]))
    labels = numpy.concatenate((labels["training"], labels["test"]))
    
    values = values.astype(float)
    
    example_names = numpy.array(["image {}".format(i + 1) for i in range(M)])
    feature_names = numpy.array(["pixel {}".format(j + 1) for j in range(N)])
    
    data_dictionary = {
        "values": values,
        "labels": labels,
        "example names": example_names,
        "feature names": feature_names,
        "split indices": split_indices
    }
    
    return data_dictionary

def loadBinarisedMNISTDataSet(paths):
    
    values = {}
    
    for kind in paths["values"]:
        values[kind] = numpy.loadtxt(paths["values"][kind])
    
    M_training = values["training"].shape[0]
    M_validation = values["validation"].shape[0]
    M_training_validation = M_training + M_validation
    M_test = values["test"].shape[0]
    M = M_training_validation + M_test
    
    split_indices = {
        "training": slice(0, M_training),
        "validation": slice(M_training, M_training_validation),
        "test": slice(M_training_validation, M)
    }
    
    values = numpy.concatenate((
        values["training"], values["validation"], values["test"]
    ))
    
    N = values.shape[1]
    
    example_names = numpy.array(["image {}".format(i + 1) for i in range(M)])
    feature_names = numpy.array(["pixel {}".format(j + 1) for j in range(N)])
    
    data_dictionary = {
        "values": values,
        "labels": None,
        "example names": example_names,
        "feature names": feature_names,
        "split indices": split_indices
    }
    
    return data_dictionary

def loadReutersDataSet(paths):
    
    topics_list = []
    body_list = []
    
    with tarfile.open(paths["all"]["full"], 'r:gz') as tarball:
        
        article_filenames = [f for f in tarball.getnames() if ".sgm" in f]
        
        for article_filename in article_filenames:
            
            with tarball.extractfile(article_filename) as article_html:
                soup = BeautifulSoup(article_html, 'html.parser')
            
            for article in soup.find_all("reuters"):
                
                topics = article.topics
                body = article.body
                
                if topics is not None and body is not None:
                    
                    topics_generator = topics.find_all("d")
                    topics_text = [topic.get_text() for topic in topics_generator]
                    
                    body_text = body.get_text()
                    
                    if len(topics_text) > 0 and len(body_text) > 0:
                        topics_list.append(topics_text)
                        body_list.append(body_text)
    
    M = len(body_list)
    
    bag_of_words, distinct_words = createBagOfWords(body_list)
    
    values = bag_of_words
    labels = numpy.array([t[0] for t in topics_list])
    example_names = numpy.array(["article {}".format(i + 1) for i in range(M)])
    feature_names = numpy.array(distinct_words)
    
    data_dictionary = {
        "values": values,
        "labels": labels,
        "example names": example_names,
        "feature names": feature_names
    }
    
    return data_dictionary

def load20NewsgroupsDataSet(paths):
    
    documents = {
        "train": [],
        "test": []
    }
    document_ids = {
        "train": [],
        "test": []
    }
    newsgroups = {
        "train": [],
        "test": []
    }
    
    with tarfile.open(paths["all"]["full"], 'r:gz') as tarball:
        
        for member in tarball:
            if member.isfile():
                
                with tarball.extractfile(member) as document_file:
                    document = document_file.read().decode("latin1") 
                
                kind, newsgroup, document_id = member.name.split(os.sep)
                
                kind = kind.split("-")[-1]
                
                documents[kind].append(document)
                document_ids[kind].append(document_id)
                newsgroups[kind].append(newsgroup)
    
    M_train = len(documents["train"])
    M_test = len(documents["test"])
    M = M_train + M_test
    
    split_indices = {
        "training": slice(0, M_train),
        "test": slice(M_train, M)
    }
    
    documents = documents["train"] + documents["test"]
    document_ids = document_ids["train"] + document_ids["test"]
    newsgroups = newsgroups["train"] + newsgroups["test"]
    
    bag_of_words, distinct_words = createBagOfWords(documents)
    
    values = bag_of_words
    labels = numpy.array(newsgroups)
    example_names = numpy.array(document_ids)
    feature_names = numpy.array(distinct_words)
    
    data_dictionary = {
        "values": values,
        "labels": labels,
        "example names": example_names,
        "feature names": feature_names,
        "split indices": split_indices
    }
    
    return data_dictionary

def loadSampleDataSet(paths):
    
    with gzip.open(paths["all"]["full"], "rb") as data_file:
        data = pickle.load(data_file)
    
    values = data["values"]
    labels = data["labels"]
    
    M, N = values.shape
    
    example_names = numpy.array(["example {}".format(i + 1) for i in range(M)])
    feature_names = numpy.array(["feature {}".format(j + 1) for j in range(N)])
    
    data_dictionary = {
        "values": values,
        "labels": labels,
        "example names": example_names,
        "feature names": feature_names
    }
    
    return data_dictionary

def createBagOfWords(documents):
    
    def findWords(text):
        lower_case_text = text.lower()
        # lower_case_text = re.sub(r"(reuter)$", "", lower_case_text)
        lower_case_text = re.sub(r"\d+[\d.,\-\(\)+]*", " DIGIT ", lower_case_text)
        words = re.compile(r"[\w'\-]+").findall(lower_case_text)
        words = [stemming.stem(word) for word in words]
        return words
    
    # Create original bag of words with one bucket per distinct word. 
    
    # List and set for saving the found words
    documents_words = list()
    distinct_words = set()
    
    # Run through documents bodies and update the list and set with words from findWords()
    for document in documents:
        
        words = findWords(document)
        
        documents_words.append(words)
        distinct_words.update(words)
    
    # Create list of the unique set of distinct words found
    distinct_words = list(distinct_words)
    
    # Create dictionary mapping words to their index in the list
    distinct_words_index = dict()
    for i, distinct_word in enumerate(distinct_words):
        distinct_words_index[distinct_word] = i
    
    # Initialize bag of words matrix with numpy's zeros()
    bag_of_words = numpy.zeros([len(documents), len(distinct_words)])
    
    # Fill out bag of words with cumulative count of word occurences
    for i, words in enumerate(documents_words):
        for word in words:
            bag_of_words[i, distinct_words_index[word]] += 1
    
    # Return bag of words matrix as a sparse representation matrix to save memory
    return bag_of_words, distinct_words

## Apply weights
def applyWeights(data, method, preprocessPath = None):
    
    weights = loadWeights(data, method, preprocessPath)
    
    return weights * data

def loadWeights(data, method, preprocessPath):
    
    if preprocessPath:
        weights_path = preprocessPath(method + "-weights")
    else:
        weights_path = None
    
    if weights_path and os.path.isfile(weights_path):
        print("Loading weights from sparse representation.")
        weights_dictionary = loadFromSparseData(weights_path)
    else:
        if method == "gini":
            weights = computeGiniIndices(data)
        elif method == "idf":
            weights = computeInverseGlobalFrequencyWeights(data)
        
        weights_dictionary = {"weights": weights}
        
        if weights_path:
            print("Saving weights in sparse representation.")
            saveAsSparseData(weights_dictionary, weights_path)
    
    return weights_dictionary["weights"]

## Compute Gini indices
def computeGiniIndices(data, epsilon = 1e-16, batch_size = 5000):
    """Calculate the Gini coefficients along last axis of a NumPy array."""
    # Based on last equation on:
    # http://www.statsdirect.com/help/default.htm#nonparametric_methods/gini.htm
    
    print("Computing Gini indices.")
    start_time = time()
    
    # Number of examples, M, and features, N
    M, N = data.shape
    
    # 1-indexing vector for each data element
    index_vector = 2 * numpy.arange(1, M + 1) - M - 1
    
    # Values cannot be 0
    data = numpy.clip(data, epsilon, data)
    
    gini_indices = numpy.zeros(N)
    
    for i in range(0, N, batch_size):
        batch = data[:, i:(i+batch_size)]
        
        # Array should be normalized and sorted frequencies over the examples
        batch = numpy.sort(batch / (numpy.sum(batch, axis = 0)), axis = 0)
        
        #Gini coefficients over the examples for each feature. 
        gini_indices[i:(i+batch_size)] = index_vector @ batch / M
    
    duration = time() - start_time
    print("Gini indices computed ({}).".format(formatDuration(duration)))
    
    return gini_indices

def normalise(data):
    return sklearn.preprocessing.normalize(data, norm = 'l2', axis = 1)

def binarise(data):
    return sklearn.preprocessing.binarize(data, threshold = 0.5)

def computeInverseGlobalFrequencyWeights(data):
    
    print("Computing IDF weights.")
    start_time = time()
    
    M = data.shape[0]
    
    global_frequencies = numpy.sum(numpy.where(data > 0, 1, 0), axis=0)
    
    idf_weights = numpy.log(M / (global_frequencies + 1))
    
    duration = time() - start_time
    print("IDF weights computed ({}).".format(formatDuration(duration)))
    
    return idf_weights

def directory(base_directory, data_set, splitting_method, splitting_fraction):
    
    data_set_directory = os.path.join(base_directory, data_set.name)
    
    if splitting_method == "default":
        splitting_method = data_set.defaultSplittingMethod()
    
    splitting_directory = "split-{}-{}".format(splitting_method,
        splitting_fraction)
    
    preprocessing_directory_parts = []
    
    if data_set.feature_selection:
        preprocessing_directory_parts.append(normaliseString(
            data_set.feature_selection))
        if data_set.feature_parameter:
            preprocessing_directory_parts.append(str(
                data_set.feature_parameter))
    
    if data_set.preprocessing_methods:
        preprocessing_directory_parts.extend(map(normaliseString,
            data_set.preprocessing_methods))
    
    if preprocessing_directory_parts:
        preprocessing_directory = "-".join(preprocessing_directory_parts)
    else:
        preprocessing_directory = "none"
    
    directory = os.path.join(data_set_directory, splitting_directory,
        preprocessing_directory)
    
    return directory
