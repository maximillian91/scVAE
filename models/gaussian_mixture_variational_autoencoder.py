# ======================================================================== #
# 
# Copyright (c) 2017 - 2018 scVAE authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# 
# ======================================================================== #

import tensorflow as tf

from models.auxiliary import (
    dense_layer, dense_layers,
    earlyStoppingStatus,
    log_reduce_exp, reduce_logmeanexp,
    trainingString, dataString,
    generateUniqueRunIDForModel,
    correctModelCheckpointPath, copyModelDirectory, removeOldCheckpoints,
    clearLogDirectory
)

from tensorflow.python.ops.nn import relu, softmax
from tensorflow import sigmoid, identity

from tensorflow.contrib.distributions import (
    Normal, Bernoulli, Categorical,
    kl_divergence
)
from distributions import distributions, latent_distributions, Categorized

import numpy
from numpy import inf
import scipy.sparse

import copy
import os, shutil
from time import time
from auxiliary import (
    checkRunID,
    formatDuration, formatTime,
    normaliseString, capitaliseString
)

from data import DataSet
from analysis import analyseIntermediateResults, accuracy
from miscellaneous.prediction import mapClusterIDsToLabelIDs
from auxiliary import loadLearningCurves

class GaussianMixtureVariationalAutoencoder(object):
    def __init__(self, feature_size, latent_size, hidden_sizes,
        number_of_monte_carlo_samples,
        number_of_importance_samples,
        analytical_kl_term = False,
        prior_probabilities = None,
        number_of_latent_clusters = 1,
        proportion_of_free_KL_nats = 0.8,
        reconstruction_distribution = None,
        number_of_reconstruction_classes = None,
        batch_normalisation = True, 
        dropout_keep_probabilities = [],
        count_sum = True,
        number_of_warm_up_epochs = 0,
        kl_weight = 1,
        clf_weight = 1.0,
        number_of_labeled_examples = 0,
        epsilon = 1e-6,
        log_directory = "log",
        results_directory = "results"):
        
        # Class setup
        super(GaussianMixtureVariationalAutoencoder, self).__init__()
        
        self.type = "GMVAE"
        
        self.feature_size = feature_size
        self.latent_size = latent_size
        self.hidden_sizes = hidden_sizes
        
        latent_distribution = "gaussian mixture"
        self.latent_distribution_name = latent_distribution
        self.latent_distribution = copy.deepcopy(
            latent_distributions[latent_distribution]
        )
        
        if prior_probabilities:
            self.prior_probabilities_method = prior_probabilities["method"]
            self.prior_probabilities = prior_probabilities["values"]
        else:
            self.prior_probabilities_method = "uniform"
            self.prior_probabilities = None
        

        if self.prior_probabilities:
            self.K = len(self.prior_probabilities)
        else:
            self.K = number_of_latent_clusters
        
        self.number_of_latent_clusters = self.K
        self.analytical_kl_term = analytical_kl_term
        
        self.proportion_of_free_KL_nats = proportion_of_free_KL_nats

        # Dictionary holding number of samples needed for the "monte carlo" 
        # estimator and "importance weighting" during both train and test time.
        self.number_of_importance_samples = number_of_importance_samples
        self.number_of_monte_carlo_samples = number_of_monte_carlo_samples

        self.reconstruction_distribution_name = reconstruction_distribution
        self.reconstruction_distribution = distributions\
            [reconstruction_distribution]
        
        # Number of categorical elements needed for reconstruction, e.g. K+1
        self.number_of_reconstruction_classes = \
            number_of_reconstruction_classes + 1
        # K: For the sum over K-1 Categorical probabilities and the last K
        #   count distribution pdf.
        self.k_max = number_of_reconstruction_classes

        self.batch_normalisation = batch_normalisation

        # Dropout keep probabilities (p) for 3 different kinds of layers
        # Hidden layers
        self.dropout_keep_probabilities = dropout_keep_probabilities
        
        self.dropout_keep_probability_y = False
        self.dropout_keep_probability_z = False
        self.dropout_keep_probability_x = False
        self.dropout_keep_probability_h = False
        self.dropout_parts = []
        if isinstance(dropout_keep_probabilities, (list, tuple)):
            p_len = len(dropout_keep_probabilities)
            if p_len >= 4:
                self.dropout_keep_probability_y = dropout_keep_probabilities[3]
            
            if p_len >= 3:
                self.dropout_keep_probability_z = dropout_keep_probabilities[2]
                
            if p_len >= 2:
                self.dropout_keep_probability_x = dropout_keep_probabilities[1]
                
            if p_len >= 1:
                self.dropout_keep_probability_h = dropout_keep_probabilities[0]
            
            for i, p in enumerate(dropout_keep_probabilities):
                if p and p != 1:
                    self.dropout_parts.append(str(p))
        else:
            self.dropout_keep_probability_h = dropout_keep_probabilities
            if dropout_keep_probabilities and dropout_keep_probabilities != 1:
                self.dropout_parts.append(str(dropout_keep_probabilities))

        self.count_sum_feature = count_sum
        self.count_sum = "constrained" in \
            self.reconstruction_distribution_name \
            or "multinomial" in self.reconstruction_distribution_name

        self.kl_weight_value = kl_weight
        self.number_of_warm_up_epochs = number_of_warm_up_epochs

        self.number_of_labeled_examples = number_of_labeled_examples
        self.clf_weight_value = clf_weight

        self.epsilon = epsilon
        
        self.base_log_directory = log_directory
        self.base_results_directory = results_directory
        
        # Early stopping
        self.early_stopping_rounds = 10
        self.stopped_early = None
        
        # Graph setup
        self.config = tf.ConfigProto()
        self.config.gpu_options.allow_growth = True
        self.graph = tf.Graph()
        
        self.parameter_summary_list = []
        
        with self.graph.as_default():
            
            self.x = tf.placeholder(tf.float32, [None, self.feature_size], 'X')
            self.t = tf.placeholder(tf.float32, [None, self.feature_size], 'T')
            self.labels = tf.placeholder(tf.int32, [None, self.K], 'LABELS')
            self.clf_mask = tf.placeholder(tf.int32, [None], 'CLF_MASK')
            self.clf_weight = tf.placeholder(tf.float32, [], 'CLF_WEIGHT')
            
            self.learning_rate = tf.placeholder(tf.float32, [],
                'learning_rate')
            
            self.kl_weight = tf.constant(
                self.kl_weight_value,
                dtype=tf.float32,
                name='kl_weight'
            )
            
            self.warm_up_weight = tf.placeholder(tf.float32, [],
                'warm_up_weight')
            parameter_summary = tf.summary.scalar('warm_up_weight',
                self.warm_up_weight)
            self.parameter_summary_list.append(parameter_summary)
            
            self.total_kl_weight = tf.multiply(
                self.warm_up_weight,
                self.kl_weight, 
                name='total_kl_weight'
            )
            total_kl_weight_summary = tf.summary.scalar('total_kl_weight',
                self.total_kl_weight)
            self.parameter_summary_list.append(total_kl_weight_summary)
            
            self.is_training = tf.placeholder(tf.bool, [], 'is_training')
            
            self.S_iw = tf.placeholder(
                tf.int32,
                [],
                'number_of_iw_samples'
            )
            self.S_mc = tf.placeholder(
                tf.int32,
                [],
                'number_of_mc_samples'
            )
            
            # Sum up counts in replicated_n feature if needed
            if self.count_sum_feature:
                self.n_feature = tf.placeholder(tf.float32, [None, 1],
                    'count_sum_feature')
                self.replicated_n_feature = tf.tile(
                    self.n_feature,
                    [self.S_iw*self.S_mc, 1]
                )
            if self.count_sum:
                self.n = tf.placeholder(tf.float32, [None, 1], 'count_sum')
                self.replicated_n = tf.tile(
                    self.n,
                    [self.S_iw*self.S_mc, 1]
                )
            self.model_graph()
            self.loss()
            self.training()
            
            self.saver = tf.train.Saver(max_to_keep = 1)
    
    @property
    def name(self):
        
        # Latent parts
        
        latent_parts = [normaliseString(self.latent_distribution_name)]
        
        if "mixture" in self.latent_distribution_name:
            latent_parts.append("c_{}".format(self.K))
        
        if self.prior_probabilities_method != "uniform":
            latent_parts.append("p_" + self.prior_probabilities_method)
        
        # Reconstruction parts
        
        reconstruction_parts = [normaliseString(
            self.reconstruction_distribution_name)]
        
        if self.k_max:
            reconstruction_parts.append("k_{}".format(self.k_max))
        
        if self.count_sum_feature:
            reconstruction_parts.append("sum")
        
        reconstruction_parts.append("l_{}".format(self.latent_size))
        reconstruction_parts.append(
            "h_" + "_".join(map(str, self.hidden_sizes)))
        
        reconstruction_parts.append("mc_{}".format(
            self.number_of_monte_carlo_samples["training"]))
        reconstruction_parts.append("iw_{}".format(
            self.number_of_importance_samples["training"]))
        
        if self.analytical_kl_term:
            reconstruction_parts.append("kl")
        
        if self.batch_normalisation:
            reconstruction_parts.append("bn")

        if len(self.dropout_parts) > 0:
            reconstruction_parts.append(
                "dropout_" + "_".join(self.dropout_parts))
        
        if self.kl_weight_value != 1:
            reconstruction_parts.append("klw_{}".format(
                self.kl_weight_value))

        if self.clf_weight_value != 1:
            reconstruction_parts.append("clfw_{}".format(
                self.clf_weight_value))

        if self.number_of_labeled_examples > 0:
            reconstruction_parts.append("nl_{}".format(
                self.number_of_labeled_examples))
        
        if self.number_of_warm_up_epochs:
            reconstruction_parts.append("wu_{}".format(
                self.number_of_warm_up_epochs))
        
        if self.proportion_of_free_KL_nats:
            reconstruction_part.append("fn_{}".format(
                self.proportion_of_free_KL_nats))
        
        # Complete name
        
        latent_part = "-".join(latent_parts)
        reconstruction_part = "-".join(reconstruction_parts)
        
        model_name = os.path.join(self.type, latent_part, reconstruction_part)
        
        return model_name
    
    def logDirectory(self, base = None, run_id = None,
        early_stopping = False, best_model = False):
        
        if not base:
            base = self.base_log_directory
        
        log_directory = os.path.join(base, self.name)
        
        if run_id:
            run_id = checkRunID(run_id)
            log_directory = os.path.join(
                log_directory,
                "run_{}".format(run_id)
            )
        
        if early_stopping and best_model:
            raise ValueError(
                "Early-stopping model and best model are mutually exclusive."
            )
        elif early_stopping:
            log_directory = os.path.join(log_directory, "early_stopping")
        elif best_model:
            log_directory = os.path.join(log_directory, "best")
        
        return log_directory
    
    @property
    def title(self):
        
        title = model.type
        
        configuration = [
            self.reconstruction_distribution_name.capitalize(),
            "$l = {}$".format(self.latent_size),
            "$h = \\{{{}\\}}$".format(", ".join(map(str, self.hidden_sizes)))
        ]
        
        if self.k_max:
            configuration.append("$k_{{\\mathrm{{max}}}} = {}$".format(
                self.k_max))
        
        if self.count_sum_feature:
            configuration.append("CS")
        
        if self.batch_normalisation:
            configuration.append("BN")
        
        if self.kl_weight_value != 1:
            configuration.append(r"$w_\mathrm{KL} = {}$".format(
                self.kl_weight))
        
        if self.number_of_warm_up_epochs:
            configuration.append("$W = {}$".format(
                self.number_of_warm_up_epochs))
        
        title += " (" + ", ".join(configuration) + ")"
        
        return title
    
    @property
    def description(self):
        
        description_parts = ["Model setup:"]
        
        description_parts.append("type: {}".format(self.type))
        description_parts.append("feature size: {}".format(self.feature_size))
        description_parts.append("latent size: {}".format(self.latent_size))
        description_parts.append("hidden sizes: {}".format(", ".join(
            map(str, self.hidden_sizes))))
        
        description_parts.append("latent distribution: " +
            self.latent_distribution_name)
        if "mixture" in self.latent_distribution_name:
            description_parts.append("latent clusters: {}".format(
                self.K))
            description_parts.append("prior probabilities: "
                + self.prior_probabilities_method)
        
        description_parts.append("reconstruction distribution: " +
            self.reconstruction_distribution_name)
        if self.k_max > 0:
            description_parts.append(
                "reconstruction classes: {}".format(self.k_max) +
                " (including 0s)"
            )
        
        mc_train = self.number_of_monte_carlo_samples["training"]
        mc_eval = self.number_of_monte_carlo_samples["evaluation"]
        
        if mc_train > 1 or mc_eval > 1:
            mc = "Monte Carlo samples: {}".format(mc_train)
            if mc_eval != mc_train:
                mc += " (training), {} (evaluation)".format(mc_eval)
            description_parts.append(mc)
        
        iw_train = self.number_of_importance_samples["training"]
        iw_eval = self.number_of_importance_samples["evaluation"]
        
        if iw_train > 1 or iw_eval > 1:
            iw = "importance samples: {}".format(iw_train)
            if iw_eval != iw_train:
                iw += " (training), {} (evaluation)".format(iw_eval)
            description_parts.append(iw)
        
        if self.kl_weight_value != 1:
            description_parts.append("KL weigth: {}".format(
                self.kl_weight_value
            ))
        
        if self.analytical_kl_term:
            description_parts.append("using analytical KL term")
        
        if self.batch_normalisation:
            description_parts.append("using batch normalisation")

        if self.number_of_warm_up_epochs:
            description_parts.append("using linear warm-up weighting for " + \
                "the first {} epochs".format(self.number_of_warm_up_epochs))

        if self.proportion_of_free_KL_nats:
            description_parts.append("using free nats for y KL divergence " + \
                " of proportion: {}".format(self.proportion_of_free_KL_nats))

        if len(self.dropout_parts) > 0:
            description_parts.append("dropout keep probability: {}".format(
                ", ".join(self.dropout_parts)))

        if self.count_sum_feature:
            description_parts.append("using count sums")
        
        if self.early_stopping_rounds:
            description_parts.append("early stopping: " +
                "after {} epoch with no improvements".format(
                    self.early_stopping_rounds))
        
        description = "\n    ".join(description_parts)
        
        return description
    
    @property
    def parameters(self, trainable = True):
        
        if trainable:
            
            parameters_string_parts = ["Trainable parameters"]
            
            with self.graph.as_default():
                trainable_parameters = tf.trainable_variables()
            
            width = max(map(len, [p.name for p in trainable_parameters]))
            
            for parameter in trainable_parameters:
                parameters_string_parts.append("{:{}}  {}".format(
                    parameter.name, width, parameter.get_shape()))
            
            parameters_string = "\n    ".join(parameters_string_parts)
        
        else:
            raise NotImplementedError("Can only return trainable parameters.")
        
        return parameters_string
    
    def q_z_given_x_y_graph(self, x, y,
        distribution_name = "modified gaussian", reuse = False):
        
        ## Encoder for q(z|x,y_i=1) = N(mu(x,y_i=1), sigma^2(x,y_i=1))
        with tf.variable_scope("Q"):
            distribution = distributions[distribution_name]
            xy = tf.concat((self.x, y), axis=-1)
            encoder = dense_layers(
                inputs = xy,
                num_outputs = self.hidden_sizes,
                activation_fn = relu,
                batch_normalisation = self.batch_normalisation, 
                is_training = self.is_training,
                input_dropout_keep_probability =
                    self.dropout_keep_probability_x,
                hidden_dropout_keep_probability =
                    self.dropout_keep_probability_h,
                scope = "ENCODER",
                layer_name = "LAYER",
                reuse = reuse
            )

            with tf.variable_scope(normaliseString(distribution_name).upper()):
                # Loop over and add parameter layers to theta dict.
                theta = {}
                for parameter in distribution["parameters"]:
                    parameter_activation_function = \
                        distribution["parameters"]\
                        [parameter]["activation function"]
                    p_min, p_max = \
                        distribution["parameters"][parameter]["support"]
                    theta[parameter] = tf.expand_dims(tf.expand_dims(
                        dense_layer(
                            inputs = encoder,
                            num_outputs = self.latent_size,
                            activation_fn = lambda x: tf.clip_by_value(
                                parameter_activation_function(x),
                                p_min + self.epsilon,
                                p_max - self.epsilon
                            ),
                            is_training = self.is_training,
                            dropout_keep_probability =
                                self.dropout_keep_probability_h,
                            scope = parameter.upper(),
                            reuse = reuse
                    ), 0), 0)

                ### Parameterise:
                q_z_given_x_y = distribution["class"](theta)

                ### Analytical mean:
                z_mean = q_z_given_x_y.mean()

                ### Sampling of
                    ### 1st dim.: importance weighting samples
                    ### 2nd dim.: monte carlo samples
                z_samples = q_z_given_x_y.sample(
                            self.S_iw * self.S_mc
                            )
                z = tf.cast(
                    tf.reshape(
                        z_samples,
                        [-1, self.latent_size]
                    ), tf.float32)
        return q_z_given_x_y, z_mean, z

    def p_z_given_y_graph(self, y, distribution_name = "modified gaussian",
        reuse = False):
        
        with tf.variable_scope("P"):
            with tf.variable_scope(normaliseString(distribution_name).upper()):
                distribution = distributions[distribution_name]
                # Loop over and add parameter layers to theta dict.
                theta = {}
                for parameter in distribution["parameters"]:
                    parameter_activation_function = \
                        distribution["parameters"]\
                        [parameter]["activation function"]
                    p_min, p_max = \
                        distribution["parameters"][parameter]["support"]
                    theta[parameter] = tf.expand_dims(tf.expand_dims(
                        dense_layer(
                            inputs = y,
                            num_outputs = self.latent_size,
                            activation_fn = lambda x: tf.clip_by_value(
                                parameter_activation_function(x),
                                p_min + self.epsilon,
                                p_max - self.epsilon
                            ),
                            is_training = self.is_training,
                            dropout_keep_probability =
                                self.dropout_keep_probability_y,
                            scope = parameter.upper(),
                            reuse = reuse
                    ), 0), 0)

                p_z_given_y = distribution["class"](theta)
                p_z_mean = tf.reduce_mean(p_z_given_y.mean())
        return p_z_given_y, p_z_mean

    def q_y_given_x_graph(self, x, distribution_name = "categorical",
        reuse = False):
        
        with tf.variable_scope(distribution_name.upper()):
            distribution = distributions[distribution_name]
            ## Encoder
            encoder = dense_layers(
                inputs = self.x,
                num_outputs = self.hidden_sizes,
                activation_fn = relu,
                batch_normalisation = self.batch_normalisation,
                is_training = self.is_training,
                input_dropout_keep_probability =
                    self.dropout_keep_probability_x,
                hidden_dropout_keep_probability =
                    self.dropout_keep_probability_h,
                scope = "ENCODER",
                layer_name = "LAYER",
                reuse = reuse
            )
            # Loop over and add parameter layers to theta dict.
            theta = {}
            for parameter in distribution["parameters"]:
                parameter_activation_function = \
                    distribution["parameters"]\
                    [parameter]["activation function"]
                p_min, p_max = distribution["parameters"][parameter]["support"]
                theta[parameter] = dense_layer(
                        inputs = encoder,
                        num_outputs = self.K,
                        activation_fn = lambda x: tf.clip_by_value(
                            parameter_activation_function(x),
                            p_min + self.epsilon,
                            p_max - self.epsilon
                        ),
                        is_training = self.is_training,
                        dropout_keep_probability =
                            self.dropout_keep_probability_h,
                        scope = parameter.upper(),
                        reuse = reuse
                )
            ### Parameterise q(y|x) = Cat(pi(x))
            q_y_given_x = distribution["class"](theta)
        return q_y_given_x

    def p_x_given_z_graph(self, z, reuse = False):
        # Decoder - Generative model, p(x|z)
        
        # Make sure we use a replication pr. sample of the feature sum, 
        # when adding this to the features.  
        if self.count_sum_feature:
            decoder = tf.concat([z, self.replicated_n_feature], axis = -1,
                name = 'Z_N')
        else:
            decoder = z
        
        decoder = dense_layers(
            inputs = decoder,
            num_outputs = self.hidden_sizes[::-1],
            activation_fn = relu,
            batch_normalisation = self.batch_normalisation,
            is_training = self.is_training,
            input_dropout_keep_probability = self.dropout_keep_probability_z,
            hidden_dropout_keep_probability = self.dropout_keep_probability_h,
            scope = "DECODER",
            layer_name = "LAYER",
            reuse = reuse
        )

        # Reconstruction distribution parameterisation
        
        with tf.variable_scope("DISTRIBUTION"):
            
            x_theta = {}
        
            for parameter in self.reconstruction_distribution["parameters"]:
                
                parameter_activation_function = \
                    self.reconstruction_distribution["parameters"]\
                    [parameter]["activation function"]
                p_min, p_max = \
                    self.reconstruction_distribution["parameters"]\
                    [parameter]["support"]
                
                x_theta[parameter] = dense_layer(
                    inputs = decoder,
                    num_outputs = self.feature_size,
                    activation_fn = lambda x: tf.clip_by_value(
                        parameter_activation_function(x),
                        p_min + self.epsilon,
                        p_max - self.epsilon
                    ),
                    is_training = self.is_training,
                    dropout_keep_probability = self.dropout_keep_probability_h,
                    scope = parameter.upper(),
                    reuse = reuse
                )
            
            if "constrained" in self.reconstruction_distribution_name or \
                "multinomial" in self.reconstruction_distribution_name:
                p_x_given_z = self.reconstruction_distribution["class"](
                    x_theta,
                    self.replicated_n
                )
            elif "multinomial" in self.reconstruction_distribution_name:
                p_x_given_z = self.reconstruction_distribution["class"](
                    x_theta,
                    self.replicated_n
                )
            else:
                p_x_given_z = self.reconstruction_distribution["class"](
                    x_theta
                )
            
            if self.k_max:
                x_logits = dense_layer(
                    inputs = decoder,
                    num_outputs = self.feature_size *
                        self.number_of_reconstruction_classes,
                    activation_fn = None,
                    is_training = self.is_training,
                    dropout_keep_probability = self.dropout_keep_probability_h,
                    scope = "P_K",
                    reuse = reuse
                )
                
                x_logits = tf.reshape(x_logits,
                    [-1, self.feature_size,
                        self.number_of_reconstruction_classes])
                
                p_x_given_z = Categorized(
                    dist = p_x_given_z,
                    cat = Categorical(logits = x_logits)
                )
            
            return p_x_given_z

    def model_graph(self):
        # Retrieving layers parameterising all distributions in model:
        
        # Y latent space
        with tf.variable_scope("Y"):
            ## p(y) = Cat(pi)
            ### shape = (1, K), so 1st batch-dim can be broadcasted to y.
            with tf.variable_scope("P"):
                if self.prior_probabilities_method != "uniform":
                        self.p_y_probabilities = \
                            tf.constant(self.prior_probabilities)
                        self.p_y_logits = tf.reshape(
                            tf.log(self.p_y_probabilities),
                            [1, self.K]
                        )
                        self.p_y = Categorical(logits = self.p_y_logits)
                else:
                    self.p_y_probabilities = tf.ones(self.K) / self.K

                self.p_y_logits = tf.reshape(
                    tf.log(self.p_y_probabilities),
                    [1, 1, self.K]
                )
            
            ## q(y|x) = Cat(pi(x))
            self.y_ = tf.fill(tf.stack(
                [tf.shape(self.x)[0],
                self.K]
                ), 0.0)
            y = [tf.add(self.y_, tf.constant(numpy.eye(
                self.K)[k], name='hot_at_{:d}'.format(k), dtype = tf.float32
            )) for k in range(self.K)]
            
            self.q_y_given_x = self.q_y_given_x_graph(self.x)
            self.q_y_logits = self.q_y_given_x.logits
            self.q_y_probabilities = tf.reduce_mean(self.q_y_given_x.probs, 0)
        
        # Z latent space
        with tf.variable_scope("Z"):
            self.q_z_given_x_y = [None]*self.K
            z_mean = [None]*self.K
            self.z = [None]*self.K
            self.p_z_given_y = [None]*self.K
            self.p_z_mean = [None]*self.K
            self.p_z_means = []
            self.p_z_variances = []
            self.q_z_means = []
            self.q_z_variances = []
            # Loop over parameter layers for all K gaussians.
            for k in range(self.K):
                if k >= 1:
                    reuse_weights = True
                else:
                    reuse_weights = False
                
                ## Latent prior distribution
                self.q_z_given_x_y[k], z_mean[k], self.z[k] = \
                self.q_z_given_x_y_graph(self.x, y[k], reuse = reuse_weights) 
                # Latent prior distribution
                self.p_z_given_y[k], self.p_z_mean[k] = \
                    self.p_z_given_y_graph(y[k], reuse = reuse_weights)
                
                self.p_z_means.append(
                    tf.reduce_mean(self.p_z_given_y[k].mean(), [0, 1, 2]))
                self.p_z_variances.append(
                    tf.square(tf.reduce_mean(self.p_z_given_y[k].stddev(),
                        [0, 1, 2])))
                    
                self.q_z_means.append(
                    tf.reduce_mean(self.q_z_given_x_y[k].mean(), [0, 1, 2]))
                self.q_z_variances.append(
                    tf.reduce_mean(tf.square(self.q_z_given_x_y[k].stddev()),
                        [0, 1, 2]))
            # self.q_y_given_x_probs = tf.one_hot(tf.argmax(
            #     self.q_y_given_x.probs, -1), self.K)
            self.q_y_given_x_probs = self.q_y_given_x.probs
            self.z_mean = tf.add_n([z_mean[k] * tf.expand_dims(
                self.q_y_given_x_probs[:, k], -1) for k in range(self.K)])
            # self.z_mean = tf.add_n([z_mean[k] * tf.expand_dims(tf.one_hot(
            # tf.argmax(self.q_y_given_x.probs, -1), self.K)[:, k], -1)
            # for k in range(self.K)])
        # Decoder for X 
        with tf.variable_scope("X"):
            self.p_x_given_z = [None]*self.K
            # self.x_given_y_mean = [None]*self.K 
            for k in range(self.K):
                if k >= 1:
                    reuse_weights = True
                else:
                    reuse_weights = False

                self.p_x_given_z[k] = self.p_x_given_z_graph(self.z[k],
                    reuse = reuse_weights)
                # self.x_given_y_mean[k] = tf.reduce_mean(
                #     tf.reshape(
                #         self.p_x_given_z[k].mean(),
                #         [self.S_iw*\
                #         self.S_mc, -1, self.feature_size]
                #     ),
                #     axis = 0
                # ) * tf.expand_dims(self.q_y_given_x.probs[:, k], -1)

            # self.p_x_mean = tf.add_n(self.x_given_y_mean)
        
        # (B, K)
        self.y_mean = self.q_y_given_x_probs
        # (R, L, Bs, K)
        self.q_y_logits = tf.reshape(self.q_y_given_x.logits, [1, -1, self.K])
        
        # Add histogram summaries for the trainable parameters
        for parameter in tf.trainable_variables():
            parameter_summary = tf.summary.histogram(parameter.name, parameter)
            self.parameter_summary_list.append(parameter_summary)
        self.parameter_summary = tf.summary.merge(self.parameter_summary_list)
    

    def loss(self):
        # Prepare replicated and reshaped arrays
        ## Replicate out batches in tiles pr. sample into: 
        ### shape = (R * L * batchsize, N_x)
        t_tiled = tf.tile(self.t, [self.S_iw*self.S_mc, 1])
        ## Reshape samples back to: 
        ### shape = (R, L, batchsize, N_z)
        z_reshaped = [
            tf.reshape(self.z[k], [self.S_iw, self.S_mc, -1, self.latent_size])
            for k in range(self.K)
        ]
        self.q_y_given_x_entropy = self.q_y_given_x.entropy()
        if self.prior_probabilities_method == "uniform":
            # H[q(y|x)] = -E_{q(y|x)}[ log(q(y|x)) ]
            # (B)
            # self.q_y_given_x_entropy = self.q_y_given_x.entropy()
            # H[q(y|x)||p(y)] = -E_{q(y|x)}[log(p(y))] = -E_{q(y|x)}[ log(1/K)]
            #                 = log(K)
            # ()
            p_y_entropy = numpy.log(self.K)
            # KL(q||p) = -E_q(y|x)[log p(y)/q(y|x)]
            #          = -E_q(y|x)[log p(y)] + E_q(y|x)[log q(y|x)]
            #          = H(q|p) - H(q)
            # (B)
            KL_y = p_y_entropy - self.q_y_given_x_entropy
        else:
            KL_y = kl_divergence(self.q_y_given_x, self.p_y)
            p_y_entropy = tf.squeeze(self.p_y.entropy())

        KL_y_threshhold = self.proportion_of_free_KL_nats * p_y_entropy

        KL_z = [None] * self.K
        KL_z_mean = [None] * self.K
        log_p_x_given_z_mean = [None] * self.K
        log_likelihood_x_z = [None] * self.K
        p_x_means = [None] * self.K
        p_x_variance_weighted = [None] * self.K
        mean_of_p_x_given_z_variances = [None] * self.K
        variance_of_p_x_given_z_means = [None] * self.K

        for k in range(self.K):
            # (R, L, B, L) --> (R, L, B)
            log_q_z_given_x_y = tf.reduce_sum(
                self.q_z_given_x_y[k].log_prob(
                    z_reshaped[k]
                ),
                axis = -1
            )
            # (R, L, B, L) --> (R, L, B)
            log_p_z_given_y = tf.reduce_sum(
                self.p_z_given_y[k].log_prob(
                    z_reshaped[k]
                ),
                axis = -1
            )
            # (R, L, B)
            KL_z[k] = log_q_z_given_x_y - log_p_z_given_y

            # (R, L, B) --> (B)
            KL_z_mean[k] = tf.reduce_mean(
                KL_z[k], 
                axis=(0,1)
            ) * self.q_y_given_x_probs[:, k]

            # (R, L, B, F)
            p_x_given_z_log_prob = self.p_x_given_z[k].log_prob(t_tiled)

            # (R, L, B, F) --> (R, L, B)
            log_p_x_given_z = tf.reshape(
                tf.reduce_sum(
                    p_x_given_z_log_prob, 
                    axis=-1
                ),
                [self.S_iw, self.S_mc, -1]
            )
            # (R, L, B) --> (B)
            log_p_x_given_z_mean[k] = tf.reduce_mean(
                log_p_x_given_z,
                axis = (0,1)
            ) * self.q_y_given_x_probs[:, k] 
            # Monte carlo estimates over: 
                # Importance weight estimates using log-mean-exp 
                    # (to avoid over- and underflow) 
                    # shape: (S_mc, batch_size)
            ##  -> shape: (batch_size)
            log_likelihood_x_z[k] = tf.reduce_mean(
                log_reduce_exp(
                    log_p_x_given_z - self.warm_up_weight * KL_z[k],
                    reduction_function=tf.reduce_mean,
                    axis = 0
                ),
                axis = 0
            ) * self.q_y_given_x_probs[:, k]

            # Importance weighted Monte Carlo estimates of: 
            # Reconstruction mean (marginalised conditional mean): 
            ##      E[x] = E[E[x|z]] = E_q(z|x)[E_p(x|z)[x]]
            ##           = E_z[p_x_given_z.mean]
            ##     \approx 1/(R*L) \sum^R_r w_r \sum^L_{l=1} p_x_given_z.mean 

            # w_{y,z} =( p(z|y)*p(y) ) / ( q(z|x,y) * q(y|x) )
            # (R, L, B) --> (R, B) --> (R, B, 1)
            # iw_weight_given_y_z = tf.exp(tf.where(self.S_iw > 1, 1.0, 0.0) *
            #     tf.reshape(-KL_z[k][:,0,:], [self.S_iw, -1, 1])
            # )

            # (R * L * B, F) --> (R, L, B, F) 
            p_x_given_z_mean = tf.reshape(
                self.p_x_given_z[k].mean(),
                [self.S_iw, self.S_mc, -1, self.feature_size]
            )

            # (R, L, B, F) --> (R, B, F) --> (B, F)
            p_x_means[k] = tf.reduce_mean(
                tf.reduce_mean(
                    p_x_given_z_mean,
                1),
            0) * tf.expand_dims(self.q_y_given_x_probs[:, k], -1)

            # Reconstruction standard deviation: 
            #      sqrt(V[x]) = sqrt(E[V[x|z]] + V[E[x|z]])
            #      = E_z[p_x_given_z.var] + E_z[(p_x_given_z.mean - E[x])^2]

            # Ê[V[x|z]] \approx q(y|x) * 1/(R*L) \sum^R_r w_r \sum^L_{l=1}
            #                 * E[x|z_lr]
            # (R * L * B, F) --> (R, L, B, F) --> (R, B, F) --> (B, F)
            mean_of_p_x_given_z_variances[k] = tf.reduce_mean(
                tf.reduce_mean(
                    tf.reshape(
                        self.p_x_given_z[k].variance(),
                        [self.S_iw, self.S_mc, -1, self.feature_size]
                    ),
                    1
                ),
                0
            ) * tf.expand_dims(self.q_y_given_x_probs[:, k], -1)

            # Estimated variance of likelihood expectation:
            # ^V[E[x|z]] = ( E[x|z_l] - Ê[x] )^2
            # (R, L, B, F) 
            variance_of_p_x_given_z_means[k] = tf.reduce_mean(
                tf.reduce_mean(
                    tf.square(
                        p_x_given_z_mean - tf.reshape(
                                p_x_means[k], 
                                [1, 1, -1, self.feature_size]
                        )
                    ),
                    1
                ),
                0
            ) * tf.expand_dims(self.q_y_given_x_probs[:, k], -1)



        # Marginalise y out in list by add_n and reshape into:
        # K*[(B, F)] --> (B, F)
        self.variance_of_p_x_given_z_mean = tf.add_n(
            variance_of_p_x_given_z_means
        )
        self.mean_of_p_x_given_z_variance = tf.add_n(
            mean_of_p_x_given_z_variances
        )
        self.p_x_stddev = tf.sqrt(
            self.mean_of_p_x_given_z_variance +\
            self.variance_of_p_x_given_z_mean
        )
        self.stddev_of_p_x_given_z_mean = tf.sqrt(
            self.variance_of_p_x_given_z_mean
        )

        self.p_x_mean = tf.add_n(p_x_means)

        # Marginalise z importance samples out
        # (R * S_mc, B, F) --> (B, F)
        # self.p_x_mean = tf.reduce_mean(p_x_mean_weight_given_z, 0)

        # log_likelihood_x_z_sum = tf.add_n(log_likelihood_x_z)

        # self.ELBO_train_modified = tf.reduce_mean(
        #     log_likelihood_x_z_sum - KL_y
        # )

        # Mask for unlabeled examples
        mask_out_labeled = 1 - self.clf_mask
        self.N_u = tf.cast(tf.reduce_sum(mask_out_labeled), 'float32')
        self.N_l = tf.cast(tf.reduce_sum(self.clf_mask), 'float32')

        alpha = tf.where(
            self.N_l > 0.0,
            self.clf_weight * (2 + 2*self.N_l),
            0.0
        )

        # Reduce to mean KL for unlabeled examples 
        # (B) --> ()
        #self.KL_z = tf.reduce_mean(tf.add_n(KL_z_mean))
        self.KL_z = tf.losses.compute_weighted_loss(
            tf.add_n(KL_z_mean),
            weights = mask_out_labeled
        )
            
        #self.KL_y = tf.reduce_mean(KL_y)
        self.KL_y = tf.losses.compute_weighted_loss(
            KL_y,
            weights = mask_out_labeled
        )

        if self.proportion_of_free_KL_nats:
            KL_y_modified = tf.where(
                self.KL_y > KL_y_threshhold,
                self.KL_y,
                KL_y_threshhold
            )
        else:
            KL_y_modified = self.KL_y

        # Add KL divergences for monitoring learning
        self.KL = self.KL_z + self.KL_y
        self.KL_all = tf.expand_dims(self.KL, -1)

        # Reduce to expected negative reconstruction error for unlabeled examples 
        self.ENRE = tf.losses.compute_weighted_loss(
            tf.add_n(log_p_x_given_z_mean),
            weights = mask_out_labeled
        )

        self.ELBO = self.ENRE - self.KL

        # Classification error for labeled examples
        self.CLF_ERROR = tf.losses.softmax_cross_entropy(
            onehot_labels = self.labels,
            logits = tf.reduce_mean(self.q_y_logits, 0),
            weights = self.clf_mask
        )

        self.ELBO_weighted = self.ENRE - self.warm_up_weight * self.kl_weight * (
            self.KL_z + KL_y_modified
        )
        # self.ELBO_train_modified = self.ENRE - self.warm_up_weight * (
        #     self.KL_z + KL_y_modified
        # )
        # tf.add_to_collection('losses', self.ELBO)

        self.total_loss = self.ELBO_weighted - alpha * self.CLF_ERROR
        
    
    def training(self):
        
        # Create the gradient descent optimiser with the given learning rate.
        def setupTraining():
            
            # Optimizer and training objective of negative loss
            optimiser = tf.train.AdamOptimizer(self.learning_rate)
            
            # Create a variable to track the global step.
            self.global_step = tf.Variable(0, name = 'global_step',
                trainable = False)
            
            # Use the optimiser to apply the gradients that minimize the loss
            # (and also increment the global step counter) as a single training
            # step.
            # self.train_op = optimiser.minimize(
            #     -self.ELBO_train_modified,
            #     global_step = self.global_step
            # )
        
            gradients = optimiser.compute_gradients(-self.total_loss)
            # gradients = optimiser.compute_gradients(-self.ELBO_train_modified)
            clipped_gradients = [
                (tf.clip_by_value(gradient, -1., 1.), variable)
                for gradient, variable in gradients
            ]
            self.train_op = optimiser.apply_gradients(clipped_gradients,
                global_step = self.global_step)
        # Make sure that the updates of the moving_averages in batch_norm
        # layers are performed before the train_step.
        
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        
        if update_ops:
            updates = tf.group(*update_ops)
            with tf.control_dependencies([updates]):
                setupTraining()
        else:
            setupTraining()
    
    def earlyStoppingStatus(self, run_id = None):
        
        stopped_early = False
        epochs_with_no_improvement = 0
        
        early_stopping_log_directory = self.logDirectory(
            run_id = run_id,
            early_stopping = True
        )
        
        log_directory = os.path.dirname(early_stopping_log_directory)
        
        if os.path.exists(log_directory):
            
            validation_losses = loadLearningCurves(
                model = self,
                data_set_kinds = "validation",
                run_id = run_id,
                log_directory = log_directory
            )["lower_bound"]
        
            if os.path.exists(early_stopping_log_directory):
                stopped_early, epochs_with_no_improvement = \
                    earlyStoppingStatus(
                        validation_losses,
                        self.early_stopping_rounds
                    )
        
        return stopped_early, epochs_with_no_improvement
    
    def train(self, training_set, validation_set = None,
        number_of_epochs = 100, batch_size = 100, learning_rate = 1e-3,
        plotting_interval = None,
        run_id = None, new_run = False, reset_training = False,
        temporary_log_directory = None):
        
        # Setup
        
        start_time = time()
        
        if run_id:
            run_id = checkRunID(run_id)
            new_run = True
        elif new_run:
            run_id = generateUniqueRunIDForModel(
                model = self,
                timestamp = start_time
            )
        
        if run_id:
            model_string = "model for run {}".format(run_id)
        else:
            model_string = "model"
        
        # Remove model run if prompted
        
        permanent_log_directory = self.logDirectory(run_id=run_id)
        
        if reset_training and os.path.exists(permanent_log_directory):
            clearLogDirectory(permanent_log_directory)
        
        ## Logging
        
        status = {
            "completed": False,
            "message": None,
            "epochs trained": None,
            "start time": formatTime(start_time),
            "training duration": None,
            "last epoch duration": None,
            "learning rate": learning_rate,
            "batch size": batch_size
        }
        
        ## Earlier model
        
        old_checkpoint = tf.train.get_checkpoint_state(permanent_log_directory)
        
        if old_checkpoint:
            epoch_start = int(os.path.basename(
                old_checkpoint.model_checkpoint_path).split('-')[-1])
        else:
            epoch_start = 0
        
        ## Log directories
        
        if temporary_log_directory:
            
            log_directory = self.logDirectory(
                base = temporary_log_directory,
                run_id = run_id
            )
            early_stopping_log_directory = self.logDirectory(
                base = temporary_log_directory,
                run_id = run_id,
                early_stopping = True
            )
            best_model_log_directory = self.logDirectory(
                base = temporary_log_directory,
                run_id = run_id,
                best_model = True
            )
            
            temporary_checkpoint = \
                tf.train.get_checkpoint_state(log_directory)
    
            if temporary_checkpoint:
                temporary_epoch_start = int(os.path.basename(
                    temporary_checkpoint.model_checkpoint_path
                ).split('-')[-1])
            else:
                temporary_epoch_start = 0
            
            if temporary_epoch_start > epoch_start:
                epoch_start = temporary_epoch_start
                replace_temporary_directory = False
            else:
                replace_temporary_directory = True
            
        else:
            log_directory = self.logDirectory(run_id=run_id)
            early_stopping_log_directory = self.logDirectory(
                run_id = run_id,
                early_stopping = True
            )
            best_model_log_directory = self.logDirectory(
                run_id = run_id,
                best_model = True
            )
        
        ## Training message
        
        data_string = dataString(
            data_set = training_set,
            reconstruction_distribution_name =
                self.reconstruction_distribution_name
        )
        training_string = trainingString(
            model_string,
            epoch_start = epoch_start,
            number_of_epochs = number_of_epochs,
            data_string = data_string
        )
        
        ## Stop, if model is already trained
        
        if epoch_start >= number_of_epochs:
            print(training_string)
            print()
            status["completed"] = True
            status["training duration"] = "nan"
            status["last epoch duration"] = "nan"
            status["epochs trained"] = "{}-{}".format(epoch_start,
                number_of_epochs)
            return status, run_id
        
        ## Copy log directory to temporary location, if necessary
        
        if temporary_log_directory \
            and os.path.exists(permanent_log_directory) \
            and replace_temporary_directory:
            
            print("Copying log directory to temporary directory.")
            copying_time_start = time()
            
            if os.path.exists(log_directory):
                shutil.rmtree(log_directory)
            
            shutil.copytree(permanent_log_directory, log_directory)
            
            copying_duration = time() - copying_time_start
            print("Log directory copied ({}).".format(formatDuration(
                copying_duration)))
            
            print()
        
        ## New model
        
        checkpoint_file = os.path.join(log_directory, 'model.ckpt')
        
        ## Data
        
        print("Preparing data.")
        preparing_data_time_start = time()
        
        ## Features
        
        ### Count sum for distributions
        if self.count_sum:
            n_train = training_set.count_sum
            if validation_set:
                n_valid = validation_set.count_sum
        
        ### Normalised count sum as a feature to the decoder
        if self.count_sum_feature:
            n_feature_train = training_set.normalised_count_sum
            if validation_set:
                n_feature_valid = validation_set.normalised_count_sum
        
        ### Numbers of examples for data subsets
        M_train = training_set.number_of_examples
        if validation_set:
            M_valid = validation_set.number_of_examples
        
        ### Preprocessing function at every epoch
        noisy_preprocess = training_set.noisy_preprocess
        
        ### Input and output
        if not noisy_preprocess:
            
            if training_set.has_preprocessed_values:
                x_train = training_set.preprocessed_values
                if validation_set:
                    x_valid = validation_set.preprocessed_values
            else:
                x_train = training_set.values
                if validation_set:
                    x_valid = validation_set.values
            
            if self.reconstruction_distribution_name == "bernoulli":
                t_train = training_set.binarised_values
                if validation_set:
                    t_valid = validation_set.binarised_values
            else:
                t_train = training_set.values
                if validation_set:
                    t_valid = validation_set.values
        
        ### Labels
        
        if training_set.has_labels:
            
            class_names_to_class_ids = numpy.vectorize(lambda class_name:
                training_set.class_name_to_class_id[class_name])
        
            training_label_ids = class_names_to_class_ids(training_set.labels)

            # Onehot encode labels
            labels_train = numpy.zeros(
                (training_set.number_of_examples,
                training_set.number_of_classes)
            )

            labels_train[
                numpy.arange(training_set.number_of_examples),
                training_label_ids] = 1

            mask_train = numpy.zeros(training_set.number_of_examples)
            mask_train[
                numpy.random.RandomState(seed=42).permutation(
                    training_set.number_of_examples)[
                        :self.number_of_labeled_examples
                    ]
            ] = 1

            if validation_set:
                validation_label_ids = class_names_to_class_ids(
                    validation_set.labels)

                # Onehot encode labels
                labels_valid = numpy.zeros(
                    (validation_set.number_of_examples,
                    validation_set.number_of_classes)
                )

                labels_valid[
                    numpy.arange(validation_set.number_of_examples),
                    validation_label_ids] = 1

                mask_valid = numpy.zeros(validation_set.number_of_examples)
                mask_valid[
                    numpy.random.RandomState(seed=42).permutation(
                        validation_set.number_of_examples)[
                            :self.number_of_labeled_examples
                        ]
                ] = 1
        
            if training_set.excluded_classes:
                excluded_class_ids = \
                    class_names_to_class_ids(training_set.excluded_classes)
            else:
                excluded_class_ids = []
        
        ### Superset labels
        
        if training_set.has_superset_labels:
        
            superset_class_names_to_superset_class_ids = numpy.vectorize(
                lambda superset_class_name:
                    training_set.superset_class_name_to_superset_class_id\
                        [superset_class_name]
            )
        
            training_superset_label_ids = \
                superset_class_names_to_superset_class_ids(
                    training_set.superset_labels)
            if validation_set:
                validation_superset_label_ids =  \
                    superset_class_names_to_superset_class_ids(
                        validation_set.superset_labels)
            
            if training_set.excluded_superset_classes:
                excluded_superset_class_ids = \
                    superset_class_names_to_superset_class_ids(
                        training_set.excluded_superset_classes)
            else:
                excluded_superset_class_ids = []
        
        preparing_data_duration = time() - preparing_data_time_start
        print("Data prepared ({}).".format(formatDuration(
            preparing_data_duration)))
        print()
        
        ## Display intervals during every epoch
        steps_per_epoch = numpy.ceil(M_train / batch_size)
        output_at_step = numpy.round(numpy.linspace(0, steps_per_epoch, 11))
        
        ## Learning curves
        
        learning_curves = {
            "training": {
                "lower_bound": [],
                "reconstruction_error": [],
                "kl_divergence_z": [],
                "kl_divergence_y": [],
                "clf_error": []
            }
        }
        if validation_set:
            learning_curves["validation"] = {
                "lower_bound": [],
                "reconstruction_error": [],
                "kl_divergence_z": [],
                "kl_divergence_y": [],
                "clf_error": []
            }
        
        with tf.Session(graph = self.graph, config=self.config) as session:
            
            parameter_summary_writer = tf.summary.FileWriter(
                log_directory)
            training_summary_writer = tf.summary.FileWriter(
                os.path.join(log_directory, "training"))
            if validation_set:
                validation_summary_writer = tf.summary.FileWriter(
                    os.path.join(log_directory, "validation"))
            
            # Initialisation
            
            checkpoint = tf.train.get_checkpoint_state(log_directory)
            
            if checkpoint:
                print("Restoring earlier model parameters.")
                restoring_time_start = time()
                
                model_checkpoint_path = correctModelCheckpointPath(
                    checkpoint.model_checkpoint_path,
                    log_directory
                )
                self.saver.restore(session, model_checkpoint_path)
                epoch_start = int(os.path.split(model_checkpoint_path)[-1]
                    .split('-')[-1])
                
                if validation_set:
                    ELBO_valid_learning_curve = loadLearningCurves(
                        model = self,
                        data_set_kinds = "validation",
                        run_id = run_id,
                        log_directory = log_directory
                    )["lower_bound"]
                
                    ELBO_valid_maximum = ELBO_valid_learning_curve.max()
                    ELBO_valid_prev = ELBO_valid_learning_curve[-1]
                
                    self.stopped_early, epochs_with_no_improvement = \
                        self.earlyStoppingStatus(run_id = run_id)
                    ELBO_valid_early_stopping = ELBO_valid_learning_curve[
                        -1 - epochs_with_no_improvement]
                
                restoring_duration = time() - restoring_time_start
                print("Earlier model parameters restored ({}).".format(
                    formatDuration(restoring_duration)))
                print()
            else:
                print("Initialising model parameters.")
                initialising_time_start = time()
                
                session.run(tf.global_variables_initializer())
                parameter_summary_writer.add_graph(session.graph)
                epoch_start = 0
                
                if validation_set:
                    ELBO_valid_maximum = - numpy.inf
                    ELBO_valid_prev = - numpy.inf
                    epochs_with_no_improvement = 0
                    ELBO_valid_early_stopping = - numpy.inf
                    self.stopped_early = False
                
                initialising_duration = time() - initialising_time_start
                print("Model parameters initialised ({}).".format(
                    formatDuration(initialising_duration)))
                print()
            
            status["epochs trained"] = "{}-{}".format(epoch_start, number_of_epochs)
            
            # Training loop
            
            print(training_string)
            print()
            training_time_start = time()
            
            for epoch in range(epoch_start, number_of_epochs):
                
                if noisy_preprocess:
                    print("Noisily preprocess values.")
                    noisy_time_start = time()
                    
                    x_train = noisy_preprocess(
                        training_set.values)
                    t_train = x_train
                    
                    if validation_set:
                        x_valid = noisy_preprocess(
                            validation_set.values)
                        t_valid = x_valid
                    
                    noisy_duration = time() - noisy_time_start
                    print("Values noisily preprocessed ({}).".format(
                        formatDuration(noisy_duration)))
                    print()
                
                epoch_time_start = time()
                
                if self.number_of_warm_up_epochs:
                    warm_up_weight = float(min(
                        epoch / (self.number_of_warm_up_epochs), 1.0))
                else:
                    warm_up_weight = 1.0
                
                shuffled_indices = numpy.random.permutation(M_train)
                
                for i in range(0, M_train, batch_size):
                    
                    # Internal setup
                    
                    step_time_start = time()
                    
                    step = session.run(self.global_step)
                    
                    # Prepare batch
                    
                    batch_indices = shuffled_indices[i:(i + batch_size)]
                    
                    x_batch = x_train[batch_indices].toarray()
                    t_batch = t_train[batch_indices].toarray()
                    labels_batch = labels_train[batch_indices]
                    mask_batch = mask_train[batch_indices]
                    
                    feed_dict_batch = {
                        self.x: x_batch,
                        self.t: t_batch,
                        self.labels: labels_batch,
                        self.clf_mask: mask_batch,
                        self.clf_weight: self.clf_weight_value,
                        self.is_training: True,
                        self.learning_rate: learning_rate, 
                        self.warm_up_weight: warm_up_weight,
                        self.S_iw:
                            self.number_of_importance_samples["training"],
                        self.S_mc:
                            self.number_of_monte_carlo_samples["training"]
                    }
                    
                    if self.count_sum:
                        feed_dict_batch[self.n] = n_train[batch_indices]

                    if self.count_sum_feature:
                        feed_dict_batch[self.n_feature] = \
                            n_feature_train[batch_indices]
                    
                    # Run the stochastic batch training operation
                    _, batch_loss, batch_clf_err = session.run(
                        [self.train_op, self.ELBO, self.CLF_ERROR],
                        feed_dict = feed_dict_batch
                    )
                    
                    # Compute step duration
                    step_duration = time() - step_time_start
                    
                    # Print evaluation and output summaries
                    if (step + 1 - steps_per_epoch * epoch) in output_at_step:
                        
                        print('Step {:d} ({}): {:.5g} + {:.5g}.'.format(
                            int(step + 1), formatDuration(step_duration),
                            batch_loss, batch_clf_err))
                        
                        if numpy.isnan(batch_loss):
                            status["completed"] = False
                            status["message"] = "loss became nan"
                            status["training duration"] = formatDuration(
                                time() - training_time_start)
                            status["last epoch duration"] = formatDuration(
                                time() - epoch_time_start)
                            return status, run_id
                
                print()
                
                epoch_duration = time() - epoch_time_start
                
                print("Epoch {} ({}):".format(epoch + 1,
                    formatDuration(epoch_duration)))

                # With warmup or not
                if warm_up_weight < 1:
                    print('    Warm-up weight: {:.2g}'.format(warm_up_weight))

                # Export parameter summaries
                parameter_summary_string = session.run(
                    self.parameter_summary,
                    feed_dict = {self.warm_up_weight: warm_up_weight}
                )
                parameter_summary_writer.add_summary(
                    parameter_summary_string, global_step = epoch + 1)
                parameter_summary_writer.flush()
                
                # Evaluation
                print('    Evaluating model.')
                
                ## Training
                
                evaluating_time_start = time()
                
                ELBO_train = 0
                KL_z_train = 0
                KL_y_train = 0
                ENRE_train = 0
                CLF_ERR_train = 0
                
                q_y_probabilities = numpy.zeros(self.K)
                q_z_means = numpy.zeros((self.K, self.latent_size))
                q_z_variances = numpy.zeros((self.K, self.latent_size))
                
                p_y_probabilities = numpy.zeros(self.K)
                p_z_means = numpy.zeros((self.K, self.latent_size))
                p_z_variances = numpy.zeros((self.K, self.latent_size))
                
                q_y_logits_train = numpy.zeros((M_train, self.K),
                    numpy.float32)
                z_mean_train = numpy.zeros((M_train, self.latent_size),
                    numpy.float32)
                q_y_entropies = numpy.zeros((M_train,),
                    numpy.float32)
                
                if "mixture" in self.latent_distribution_name: 
                    z_KL = numpy.zeros(1)                
                else:
                    z_KL = numpy.zeros(self.latent_size)
                
                for i in range(0, M_train, batch_size):
                    subset = slice(i, min(i + batch_size, M_train))
                    x_batch = x_train[subset].toarray()
                    t_batch = t_train[subset].toarray()
                    labels_batch = labels_train[subset]
                    mask_batch = mask_train[subset]
                    feed_dict_batch = {
                        self.x: x_batch,
                        self.t: t_batch,
                        self.labels: labels_batch,
                        self.clf_mask: mask_batch,
                        self.is_training: False,
                        self.warm_up_weight: 1.0,
                        self.S_iw:
                            self.number_of_importance_samples["training"],
                        self.S_mc:
                            self.number_of_monte_carlo_samples["training"]
                    }
                    if self.count_sum:
                        feed_dict_batch[self.n] = n_train[subset]

                    if self.count_sum_feature:
                        feed_dict_batch[self.n_feature] = \
                            n_feature_train[subset]
                    
                    (ELBO_i, ENRE_i, KL_z_i, KL_y_i, z_KL_i, CLF_ERR_i,
                        q_y_probabilities_i, q_z_means_i, q_z_variances_i,
                        p_y_probabilities_i, p_z_means_i, p_z_variances_i,
                        q_y_logits_train_i, z_mean_i, q_y_entropies_i) = session.run(
                        [self.ELBO, self.ENRE,  self.KL_z, self.KL_y,
                            self.KL_all, self.CLF_ERROR, self.q_y_probabilities,
                             self.q_z_means, self.q_z_variances,
                            self.p_y_probabilities, self.p_z_means,
                            self.p_z_variances, self.q_y_logits, self.z_mean,
                            self.q_y_given_x_entropy],
                        feed_dict = feed_dict_batch
                    )
                    
                    ELBO_train += ELBO_i
                    KL_z_train += KL_z_i
                    KL_y_train += KL_y_i
                    ENRE_train += ENRE_i
                    CLF_ERR_train += CLF_ERR_i
                    
                    z_KL += z_KL_i
                    
                    q_y_probabilities += numpy.array(q_y_probabilities_i)
                    q_z_means += numpy.array(q_z_means_i)
                    q_z_variances += numpy.array(q_z_variances_i)
                    
                    p_y_probabilities += numpy.array(p_y_probabilities_i)
                    p_z_means += numpy.array(p_z_means_i)
                    p_z_variances += numpy.array(p_z_variances_i)
                    
                    q_y_logits_train[subset] = q_y_logits_train_i
                    z_mean_train[subset] = z_mean_i

                    q_y_entropies[subset] = q_y_entropies_i
                
                N_train_batches = M_train / batch_size

                ELBO_train /= N_train_batches
                KL_z_train /= N_train_batches
                KL_y_train /= N_train_batches
                ENRE_train /= N_train_batches
                CLF_ERR_train /= N_train_batches

                z_KL /= N_train_batches

                q_y_probabilities /= N_train_batches
                q_z_means /= N_train_batches
                q_z_variances /= N_train_batches

                p_y_probabilities /= N_train_batches
                p_z_means /= N_train_batches
                p_z_variances /= N_train_batches
                
                learning_curves["training"]["lower_bound"].append(ELBO_train)
                learning_curves["training"]["reconstruction_error"].append(
                    ENRE_train)
                learning_curves["training"]["kl_divergence_z"].append(
                    KL_z_train)
                learning_curves["training"]["kl_divergence_y"].append(
                    KL_y_train)
                learning_curves["training"]["clf_error"].append(
                    CLF_ERR_train)
                
                ### Active learning label acquisition of max entropy point
                q_y_entropies[numpy.array(mask_train, dtype=bool)] = -1e6
                max_entropy_ind = numpy.argmax(
                    q_y_entropies,
                    0
                )
                mask_train[max_entropy_ind] = 1
                print("\tAdded labeled data point number {}/{} with index {} and label {}.\n".format(
                        numpy.sum(mask_train),
                        M_train,
                        max_entropy_ind,
                        training_set.labels[max_entropy_ind]
                    )
                )

                ### Accuracies
                
                training_cluster_ids = q_y_logits_train.argmax(axis = 1)
                
                if training_set.has_labels:
                    predicted_training_label_ids = mapClusterIDsToLabelIDs(
                        training_label_ids,
                        training_cluster_ids,
                        excluded_class_ids
                    )
                    accuracy_train = accuracy(
                        training_label_ids,
                        predicted_training_label_ids,
                        excluded_class_ids
                    )
                else:
                    accuracy_train = None
                
                if training_set.label_superset:
                    predicted_training_superset_label_ids = \
                        mapClusterIDsToLabelIDs(
                        training_superset_label_ids,
                        training_cluster_ids,
                        excluded_superset_class_ids
                    )
                    accuracy_superset_train = accuracy(
                        training_superset_label_ids,
                        predicted_training_superset_label_ids,
                        excluded_superset_class_ids
                    )
                    accuracy_display = accuracy_superset_train
                else:
                    accuracy_superset_train = None
                    accuracy_display = accuracy_train
                
                evaluating_duration = time() - evaluating_time_start
                
                ### Summaries
                
                summary = tf.Summary()
                
                #### Losses and accuracies
                summary.value.add(tag="losses/lower_bound",
                    simple_value = ELBO_train)
                summary.value.add(tag="losses/reconstruction_error",
                    simple_value = ENRE_train)
                summary.value.add(tag="losses/kl_divergence_z",
                    simple_value = KL_z_train)
                summary.value.add(tag="losses/kl_divergence_y",
                    simple_value = KL_y_train)
                summary.value.add(tag="losses/clf_error",
                    simple_value = CLF_ERR_train)               
                summary.value.add(tag="accuracy",
                    simple_value = accuracy_train)
                if accuracy_superset_train:
                    summary.value.add(tag="superset_accuracy",
                        simple_value = accuracy_superset_train)
                
                #### KL divergence
                for i in range(z_KL.size):
                    summary.value.add(tag="kl_divergence_neurons/{}".format(i),
                        simple_value = z_KL[i])
                
                #### Centroids
                if validation_set:
                    for k in range(self.K):
                        summary.value.add(
                            tag="prior/cluster_{}/probability".format(k),
                            simple_value = p_y_probabilities[k]
                        )
                        summary.value.add(
                            tag="posterior/cluster_{}/probability".format(k),
                            simple_value = q_y_probabilities[k]
                        )
                        for l in range(self.latent_size):
                            summary.value.add(
                                tag="prior/cluster_{}/mean/dimension_{}".format(
                                    k, l),
                                simple_value = p_z_means[k][l]
                            )
                            summary.value.add(
                                tag="posterior/cluster_{}/mean/dimension_{}"
                                    .format(k, l),
                                simple_value = q_z_means[k, l]
                            )
                            summary.value.add(
                                tag="prior/cluster_{}/variance/dimension_{}"\
                                    .format(k, l),
                                simple_value = p_z_variances[k][l]
                            )
                            summary.value.add(
                                tag="posterior/cluster_{}/variance/dimension_{}"\
                                    .format(k, l),
                                simple_value = q_z_variances[k, l]
                            )
                
                #### Writing
                training_summary_writer.add_summary(summary,
                    global_step = epoch + 1)
                training_summary_writer.flush()
                
                ### Printing
                evaluation_string = "    {} set ({}): ".format(
                    training_set.kind.capitalize(),
                    formatDuration(evaluating_duration)
                )
                evaluation_metrics = [
                    "ELBO: {:.5g}".format(ELBO_train),
                    "ENRE: {:.5g}".format(ENRE_train),
                    "KL_z: {:.5g}".format(KL_z_train),
                    "KL_y: {:.5g}".format(KL_y_train),
                    "CLF: {:.5g}".format(CLF_ERR_train),
                ]
                if accuracy_display:
                    evaluation_metrics.append(
                        "Acc: {:.5g}".format(accuracy_display)
                    )
                evaluation_string += ", ".join(evaluation_metrics)
                evaluation_string += "."
                
                print(evaluation_string)
                
                ## Validation
                
                if validation_set:
                    
                    evaluating_time_start = time()
                    
                    ELBO_valid = 0
                    KL_z_valid = 0
                    KL_y_valid = 0
                    ENRE_valid = 0
                    CLF_ERR_valid = 0
                    
                    q_y_probabilities = numpy.zeros(self.K)
                    q_z_means = numpy.zeros((self.K, self.latent_size))
                    q_z_variances = numpy.zeros((self.K, self.latent_size))
                    
                    p_y_probabilities = numpy.zeros(self.K)
                    p_z_means = numpy.zeros((self.K, self.latent_size))
                    p_z_variances = numpy.zeros((self.K, self.latent_size))
                    
                    q_y_logits_valid = numpy.zeros((M_valid, self.K),
                        numpy.float32)
                    z_mean_valid = numpy.zeros((M_valid, self.latent_size),
                        numpy.float32)
                    
                    for i in range(0, M_valid, batch_size):
                        subset = slice(i, min(i + batch_size, M_valid))
                        x_batch = x_valid[subset].toarray()
                        t_batch = t_valid[subset].toarray()
                        labels_batch = labels_train[subset]
                        mask_batch = mask_train[subset]
                        feed_dict_batch = {
                            self.x: x_batch,
                            self.t: t_batch,
                            self.labels: labels_batch,
                            self.clf_mask: mask_batch,
                            self.is_training: False,
                            self.warm_up_weight: 1.0,
                            self.S_iw:
                                self.number_of_importance_samples["training"],
                            self.S_mc:
                                self.number_of_monte_carlo_samples["training"]
                        }
                        if self.count_sum:
                            feed_dict_batch[self.n] = n_valid[subset]
                        
                        if self.count_sum_feature:
                            feed_dict_batch[self.n_feature] = \
                                n_feature_valid[subset]
                        
                        (ELBO_i, ENRE_i, KL_z_i, KL_y_i, CLF_ERR_i,
                            q_y_probabilities_i, q_z_means_i, q_z_variances_i,
                            p_y_probabilities_i, p_z_means_i, p_z_variances_i,
                            q_y_logits_i, z_mean_i) = session.run(
                            [self.ELBO, self.ENRE, self.KL_z, self.KL_y,
                                self.CLF_ERROR, self.q_y_probabilities,
                                self.q_z_means, self.q_z_variances,
                                self.p_y_probabilities, self.p_z_means,
                                self.p_z_variances, self.q_y_logits, self.z_mean],
                            feed_dict = feed_dict_batch
                        )
                        
                        ELBO_valid += ELBO_i
                        KL_z_valid += KL_z_i
                        KL_y_valid += KL_y_i
                        ENRE_valid += ENRE_i
                        CLF_ERR_valid += CLF_ERR_i
                        
                        q_y_probabilities += numpy.array(q_y_probabilities_i)
                        q_z_means += numpy.array(q_z_means_i)
                        q_z_variances += numpy.array(q_z_variances_i)
                        
                        p_y_probabilities += numpy.array(p_y_probabilities_i)
                        p_z_means += numpy.array(p_z_means_i)
                        p_z_variances += numpy.array(p_z_variances_i)
                        
                        q_y_logits_valid[subset] = q_y_logits_i
                        z_mean_valid[subset] = z_mean_i 
                    
                    N_valid_batches = M_valid / batch_size

                    ELBO_valid /= N_valid_batches
                    KL_z_valid /= N_valid_batches
                    KL_y_valid /= N_valid_batches
                    ENRE_valid /= N_valid_batches
                    CLF_ERR_valid /= N_valid_batches
                    
                    q_y_probabilities /= N_valid_batches
                    q_z_means /= N_valid_batches
                    q_z_variances /= N_valid_batches
                    
                    p_y_probabilities /= N_valid_batches
                    p_z_means /= N_valid_batches
                    p_z_variances /= N_valid_batches
                    
                    learning_curves["validation"]["lower_bound"].append(ELBO_valid)
                    learning_curves["validation"]["reconstruction_error"].append(
                        ENRE_valid)
                    learning_curves["validation"]["kl_divergence_z"].append(
                        KL_z_valid)
                    learning_curves["validation"]["kl_divergence_y"].append(
                        KL_y_valid)
                    learning_curves["validation"]["clf_error"].append(
                        KL_y_valid)
                    
                    ### Accuracies
                    
                    validation_cluster_ids = q_y_logits_valid.argmax(axis = 1)
                    
                    if validation_set.has_labels:
                        predicted_validation_label_ids = mapClusterIDsToLabelIDs(
                            validation_label_ids,
                            validation_cluster_ids,
                            excluded_class_ids
                        )
                        accuracy_valid = accuracy(
                            validation_label_ids,
                            predicted_validation_label_ids,
                            excluded_class_ids
                        )
                    else:
                        accuracy_valid = None
                    
                    if validation_set.label_superset:
                        predicted_validation_superset_label_ids = \
                            mapClusterIDsToLabelIDs(
                            validation_superset_label_ids,
                            validation_cluster_ids,
                            excluded_superset_class_ids
                        )
                        accuracy_superset_valid = accuracy(
                            validation_superset_label_ids,
                            predicted_validation_superset_label_ids,
                            excluded_superset_class_ids
                        )
                        accuracy_display = accuracy_superset_valid
                    else:
                        accuracy_superset_valid = None
                        accuracy_display = accuracy_valid
                    
                    evaluating_duration = time() - evaluating_time_start
                    
                    ### Summaries
                    
                    summary = tf.Summary()
                    
                    #### Losses and accuracies
                    summary.value.add(tag="losses/lower_bound",
                        simple_value = ELBO_valid)
                    summary.value.add(tag="losses/reconstruction_error",
                        simple_value = ENRE_valid)
                    summary.value.add(tag="losses/kl_divergence_z",
                        simple_value = KL_z_valid)
                    summary.value.add(tag="losses/kl_divergence_y",
                        simple_value = KL_y_valid)
                    summary.value.add(tag="losses/clf_error",
                        simple_value = CLF_ERR_valid)
                    summary.value.add(tag="accuracy",
                        simple_value = accuracy_valid)
                    if accuracy_superset_valid:
                        summary.value.add(tag="superset_accuracy",
                            simple_value = accuracy_superset_valid)
                    
                    #### Centroids
                    for k in range(self.K):
                        summary.value.add(
                            tag="prior/cluster_{}/probability".format(k),
                            simple_value = p_y_probabilities[k]
                        )
                        summary.value.add(
                            tag="posterior/cluster_{}/probability".format(k),
                            simple_value = q_y_probabilities[k]
                        )
                        for l in range(self.latent_size):
                            summary.value.add(
                                tag="prior/cluster_{}/mean/dimension_{}".format(
                                    k, l),
                                simple_value = p_z_means[k][l]
                            )
                            summary.value.add(
                                tag="posterior/cluster_{}/mean/dimension_{}"
                                    .format(k, l),
                                simple_value = q_z_means[k, l]
                            )
                            summary.value.add(
                                tag="prior/cluster_{}/variance/dimension_{}"\
                                    .format(k, l),
                                simple_value = p_z_variances[k][l]
                            )
                            summary.value.add(
                                tag="posterior/cluster_{}/variance/dimension_{}"\
                                    .format(k, l),
                                simple_value = q_z_variances[k, l]
                            )
                    
                    #### Writing
                    validation_summary_writer.add_summary(summary,
                        global_step = epoch + 1)
                    validation_summary_writer.flush()
                    
                    ### Printing
                    evaluation_string = "    {} set ({}): ".format(
                        validation_set.kind.capitalize(),
                        formatDuration(evaluating_duration)
                    )
                    evaluation_metrics = [
                        "ELBO: {:.5g}".format(ELBO_valid),
                        "ENRE: {:.5g}".format(ENRE_valid),
                        "KL_z: {:.5g}".format(KL_z_valid),
                        "KL_y: {:.5g}".format(KL_y_valid),
                        "CLF: {:.5g}".format(CLF_ERR_valid),
                    ]
                    if accuracy_display:
                        evaluation_metrics.append(
                            "Acc: {:.5g}".format(accuracy_display)
                        )
                    evaluation_string += ", ".join(evaluation_metrics)
                    evaluation_string += "."
                    
                    print(evaluation_string)
                
                # Early stopping
                if validation_set and not self.stopped_early:
                    
                    if ELBO_valid < ELBO_valid_early_stopping:
                        if epochs_with_no_improvement == 0:
                            print("    Early stopping:",
                                "Validation loss did not improve",
                                "for this epoch.")
                            print("        " + \
                                "Saving model parameters for previous epoch.")
                            saving_time_start = time()
                            ELBO_valid_early_stopping = ELBO_valid
                            current_checkpoint = \
                                tf.train.get_checkpoint_state(
                                    log_directory)
                            if current_checkpoint:
                                copyModelDirectory(current_checkpoint,
                                    early_stopping_log_directory)
                            saving_duration = time() - saving_time_start
                            print("        " + 
                                "Previous model parameters saved ({})."\
                                .format(formatDuration(saving_duration)))
                        else:
                            print("    Early stopping:",
                                "Validation loss has not improved",
                                "for {} epochs.".format(
                                    epochs_with_no_improvement + 1))
                        epochs_with_no_improvement += 1
                    else:
                        if epochs_with_no_improvement > 0:
                            print("    Early stopping cancelled:",
                                "Validation loss improved.")
                        epochs_with_no_improvement = 0
                        ELBO_valid_early_stopping = ELBO_valid
                        if os.path.exists(early_stopping_log_directory):
                            shutil.rmtree(early_stopping_log_directory)
                    
                    if epochs_with_no_improvement >= \
                        self.early_stopping_rounds:
                        
                        print("    Early stopping in effect:",
                            "Previously saved model parameters is available.")
                        self.stopped_early = True
                        epochs_with_no_improvement = numpy.nan
                
                # Saving model parameters (update checkpoint)
                print('    Saving model parameters.')
                saving_time_start = time()
                self.saver.save(session, checkpoint_file,
                    global_step = epoch + 1)
                saving_duration = time() - saving_time_start
                print('    Model parameters saved ({}).'.format(
                    formatDuration(saving_duration)))
                
                # Saving best model parameters yet
                if validation_set and ELBO_valid > ELBO_valid_maximum:
                    print("    Best validation ELBO yet.",
                        "Saving model parameters as best model parameters.")
                    saving_time_start = time()
                    ELBO_valid_maximum = ELBO_valid
                    current_checkpoint = \
                        tf.train.get_checkpoint_state(log_directory)
                    if current_checkpoint:
                        copyModelDirectory(current_checkpoint,
                            best_model_log_directory)
                    removeOldCheckpoints(best_model_log_directory)
                    saving_duration = time() - saving_time_start
                    print('    Best model parameters saved ({}).'.format(
                        formatDuration(saving_duration)))
                
                print()
                
                # Plot latent validation values
                if plotting_interval is None:
                    under_10 = epoch < 10
                    under_100 = epoch < 100 and (epoch + 1) % 10 == 0
                    under_1000 = epoch < 1000 and (epoch + 1) % 50 == 0 
                    above_1000 = epoch > 1000 and (epoch + 1) % 100 == 0 
                    last_one = epoch == number_of_epochs - 1
                    plot_intermediate_results = under_10 \
                        or under_100 \
                        or under_1000 \
                        or above_1000 \
                        or last_one
                else: 
                    plot_intermediate_results =\
                        epoch % plotting_interval == 0

                if plot_intermediate_results:
                    for data_set in ['training', 'validation']:                  
                        if "mixture" in self.latent_distribution_name:
                            K = self.K
                            L = self.latent_size
                            p_z_covariance_matrices = numpy.empty([K, L, L])
                            q_z_covariance_matrices = numpy.empty([K, L, L])
                            for k in range(K):
                                p_z_covariance_matrices[k] = numpy.diag(
                                    p_z_variances[k])
                                q_z_covariance_matrices[k] = numpy.diag(
                                    q_z_variances[k])
                            centroids = {
                                "prior": {
                                    "probabilities": p_y_probabilities,
                                    "means": numpy.stack(p_z_means),
                                    "covariance_matrices": p_z_covariance_matrices
                                },
                                "posterior": {
                                    "probabilities": q_y_probabilities,
                                    "means": q_z_means,
                                    "covariance_matrices": q_z_covariance_matrices
                                }
                            }
                        else:
                            centroids = None
                        
                        if validation_set and data_set == 'validation':
                            intermediate_latent_values = z_mean_valid
                            intermediate_data_set = validation_set
                            intermediate_mask = mask_valid
                        elif data_set == 'training':
                            intermediate_latent_values = z_mean_train
                            intermediate_data_set = training_set
                            intermediate_mask = mask_train
                        
                        analyseIntermediateResults(
                            learning_curves = learning_curves,
                            epoch_start = epoch_start,
                            epoch = epoch,
                            latent_values = intermediate_latent_values,
                            data_set = intermediate_data_set,
                            label_mask = intermediate_mask,
                            centroids = centroids,
                            model_name = self.name,
                            run_id = run_id,
                            model_type = self.type,
                            results_directory = self.base_results_directory
                        )
                        print()
                else:
                    analyseIntermediateResults(
                        learning_curves = learning_curves,
                        epoch_start = epoch_start,
                        model_name = self.name,
                        run_id = run_id,
                        model_type = self.type,
                        results_directory = self.base_results_directory
                    )
                    print()
                
                # Update variables for previous iteration
                if validation_set:
                    ELBO_valid_prev = ELBO_valid
            
            training_duration = time() - training_time_start
            
            print("{} trained for {} epochs ({}).".format(
                capitaliseString(model_string),
                number_of_epochs,
                formatDuration(training_duration))
            )
            print()
            
            # Clean up
            
            removeOldCheckpoints(log_directory)
            
            if temporary_log_directory:
                
                print("Moving log directory to permanent directory.")
                copying_time_start = time()
                
                if os.path.exists(permanent_log_directory):
                    shutil.rmtree(permanent_log_directory)
                
                shutil.move(log_directory, permanent_log_directory)
                
                copying_duration = time() - copying_time_start
                print("Log directory moved ({}).".format(formatDuration(
                    copying_duration)))
                
                print()
            
            status["completed"] = True
            status["training duration"] = formatDuration(training_duration)
            status["last epoch duration"] = formatDuration(epoch_duration)
            
            return status, run_id
    
    def evaluate(self, evaluation_set, evaluation_subset_indices = set(),
        batch_size = 100, predict_labels = True, run_id = None,
        use_early_stopping_model = False, use_best_model = False,
        output_versions = "all", log_results = True):
        
        # Setup
        
        if run_id:
            run_id = checkRunID(run_id)
            model_string = "model for run {}".format(run_id)
        else:
            model_string = "model"
        
        if output_versions == "all":
            output_versions = ["transformed", "reconstructed", "latent"]
        elif not isinstance(output_versions, list):
            output_versions = [output_versions]
        else:
            number_of_output_versions = len(output_versions)
            if number_of_output_versions > 3:
                raise ValueError("Can only output at most 3 sets, "
                    + "{} requested".format(number_of_output_versions))
            elif number_of_output_versions != len(set(output_versions)):
                raise ValueError("Cannot output duplicate sets, "
                    + "{} requested.".format(output_versions))
        
        evaluation_set_transformed = False
        
        ## Examples
        
        if self.count_sum:
            n_eval = evaluation_set.count_sum

        if self.count_sum_feature:
            n_feature_eval = evaluation_set.normalised_count_sum
        
        M_eval = evaluation_set.number_of_examples
        F_eval = evaluation_set.number_of_features
        
        noisy_preprocess = evaluation_set.noisy_preprocess
        
        if not noisy_preprocess:
            
            if evaluation_set.has_preprocessed_values:
                x_eval = evaluation_set.preprocessed_values
            else:
                x_eval = evaluation_set.values
            
            if self.reconstruction_distribution_name == "bernoulli":
                t_eval = evaluation_set.binarised_values
                evaluation_set_transformed = True
            else:
                t_eval = evaluation_set.values
            
        else:
            print("Noisily preprocess values.")
            noisy_time_start = time()
            x_eval = noisy_preprocess(evaluation_set.values)
            t_eval = x_eval
            evaluation_set_transformed = True
            noisy_duration = time() - noisy_time_start
            print("Values noisily preprocessed ({}).".format(
                formatDuration(noisy_duration)))
            print()
        
        ## Labels
        
        if evaluation_set.has_labels:
            
            class_names_to_class_ids = numpy.vectorize(lambda class_name:
                evaluation_set.class_name_to_class_id[class_name])
            class_ids_to_class_names = numpy.vectorize(lambda class_id:
                evaluation_set.class_id_to_class_name[class_id])
                
            evaluation_label_ids = class_names_to_class_ids(
                evaluation_set.labels)

            # Onehot encode labels
            labels_eval = numpy.zeros(
                (evaluation_set.number_of_examples,
                evaluation_set.number_of_classes)
            )

            labels_eval[
                numpy.arange(evaluation_set.number_of_examples),
                evaluation_label_ids] = 1

            mask_eval = numpy.zeros(evaluation_set.number_of_examples)
            mask_eval[
                numpy.random.RandomState(seed=42).permutation(
                    evaluation_set.number_of_examples)[
                        :self.number_of_labeled_examples
                    ]
            ] = 1
            
            if evaluation_set.excluded_classes:
                excluded_class_ids = class_names_to_class_ids(
                    evaluation_set.excluded_classes)
            else:
                excluded_class_ids = []
        
        ## Superset labels
        
        if evaluation_set.label_superset:
        
            superset_class_names_to_superset_class_ids = numpy.vectorize(
                lambda superset_class_name:
                    evaluation_set.superset_class_name_to_superset_class_id\
                        [superset_class_name]
            )
            superset_class_ids_to_superset_class_names = numpy.vectorize(
                lambda superset_class_id:
                    evaluation_set.superset_class_id_to_superset_class_name\
                        [superset_class_id]
            )
        
            evaluation_superset_label_ids = \
                superset_class_names_to_superset_class_ids(
                    evaluation_set.superset_labels)
            
            if evaluation_set.excluded_superset_classes:
                excluded_superset_class_ids = \
                    superset_class_names_to_superset_class_ids(
                        evaluation_set.excluded_superset_classes)
            else:
                excluded_superset_class_ids = []
        
        ## Other
        
        log_directory = self.logDirectory(
            run_id = run_id,
            early_stopping = use_early_stopping_model,
            best_model = use_best_model
        )
        
        checkpoint = tf.train.get_checkpoint_state(log_directory)
        
        if log_results:
            eval_summary_directory = os.path.join(log_directory, "evaluation")
            if os.path.exists(eval_summary_directory):
                shutil.rmtree(eval_summary_directory)
        
        # Evaluation
        
        with tf.Session(graph = self.graph, config=self.config) as session:
            
            if log_results:
                eval_summary_writer = tf.summary.FileWriter(
                    eval_summary_directory)
            
            if checkpoint:
                model_checkpoint_path = correctModelCheckpointPath(
                    checkpoint.model_checkpoint_path,
                    log_directory
                )
                self.saver.restore(session, model_checkpoint_path)
                epoch = int(os.path.split(model_checkpoint_path)[-1]
                    .split('-')[-1])
            else:
                print(
                    "Cannot evaluate {} when it has not been trained.".format(
                        model_string)
                )
                return [None] * len(output_versions)
            
            data_string = dataString(evaluation_set,
                self.reconstruction_distribution_name)
            print('Evaluating trained {} on {}.'.format(model_string,
                data_string))
            evaluating_time_start = time()
            
            ELBO_eval = 0
            KL_z_eval = 0
            KL_y_eval = 0
            ENRE_eval = 0
            
            if log_results:
                q_y_probabilities = numpy.zeros(self.K)
                q_z_means = numpy.zeros((self.K, self.latent_size))
                q_z_variances = numpy.zeros((self.K, self.latent_size))
                p_y_probabilities = numpy.zeros(self.K)
                p_z_means = numpy.zeros((self.K, self.latent_size))
                p_z_variances = numpy.zeros((self.K, self.latent_size))
            
            q_y_logits = numpy.zeros((M_eval, self.K))
            
            if "reconstructed" in output_versions:
                p_x_mean_eval = numpy.zeros((M_eval, F_eval), numpy.float32)
                p_x_stddev_eval = scipy.sparse.lil_matrix((M_eval, F_eval),
                    dtype = numpy.float32)
                stddev_of_p_x_given_z_mean_eval = scipy.sparse.lil_matrix(
                    (M_eval, F_eval), dtype = numpy.float32)
            
            if "latent" in output_versions:
                z_mean_eval = numpy.zeros((M_eval, self.latent_size),
                    numpy.float32)
                y_mean_eval = numpy.zeros((M_eval, self.K), numpy.float32)
            
            for i in range(0, M_eval, batch_size):
                
                indices = numpy.arange(i, min(i + batch_size, M_eval))
                
                subset_indices = numpy.array(list(
                    evaluation_subset_indices.intersection(indices)))
                feed_dict_batch = {
                    self.x: x_eval[indices].toarray(),
                    self.t: t_eval[indices].toarray(),
                    self.labels: labels_eval[indices],
                    self.clf_mask: mask_eval[indices],
                    self.is_training: False,
                    self.warm_up_weight: 1.0,
                    self.S_iw:
                        self.number_of_importance_samples["evaluation"],
                    self.S_mc:
                        self.number_of_monte_carlo_samples["evaluation"]
                }
                if self.count_sum:
                    feed_dict_batch[self.n] = n_eval[indices]

                if self.count_sum_feature:
                    feed_dict_batch[self.n_feature] = n_feature_eval[indices]

                (ELBO_i, ENRE_i, KL_z_i, KL_y_i,
                    q_y_probabilities_i, q_z_means_i, q_z_variances_i,
                    p_y_probabilities_i, p_z_means_i, p_z_variances_i,
                    q_y_logits_i, p_x_mean_i,
                    p_x_stddev_i, stddev_of_p_x_given_z_mean_i,
                    y_mean_i, z_mean_i) = session.run(
                        [
                            self.ELBO, self.ENRE, self.KL_z, self.KL_y,
                            self.q_y_probabilities, self.q_z_means, 
                            self.q_z_variances, self.p_y_probabilities, 
                            self.p_z_means, self.p_z_variances,
                            self.q_y_logits, self.p_x_mean,
                            self.p_x_stddev, self.stddev_of_p_x_given_z_mean,
                            self.y_mean, self.z_mean
                        ],
                        feed_dict = feed_dict_batch
                    )
                
                ELBO_eval += ELBO_i
                KL_z_eval += KL_z_i
                KL_y_eval += KL_y_i
                ENRE_eval += ENRE_i
                
                if log_results:
                    q_y_probabilities += numpy.array(q_y_probabilities_i)
                    q_z_means += numpy.array(q_z_means_i)
                    q_z_variances += numpy.array(q_z_variances_i)
                    p_y_probabilities += numpy.array(p_y_probabilities_i)
                    p_z_means += numpy.array(p_z_means_i)
                    p_z_variances += numpy.array(p_z_variances_i)
                
                q_y_logits[indices] = q_y_logits_i
                
                if "reconstructed" in output_versions:
                    p_x_mean_eval[indices] = p_x_mean_i 
                
                    if subset_indices.size > 0:
                        p_x_stddev_eval[subset_indices] = \
                            p_x_stddev_i[subset_indices - i]
                        stddev_of_p_x_given_z_mean_eval[subset_indices] = \
                            stddev_of_p_x_given_z_mean_i[subset_indices - i]
                
                if "latent" in output_versions:
                    y_mean_eval[indices] = y_mean_i 
                    z_mean_eval[indices] = z_mean_i 
            
            ELBO_eval /= M_eval / batch_size
            KL_z_eval /= M_eval / batch_size
            KL_y_eval /= M_eval / batch_size
            ENRE_eval /= M_eval / batch_size
            
            if log_results:
                q_y_probabilities /= M_eval / batch_size
                q_z_means /= M_eval / batch_size
                q_z_variances /= M_eval / batch_size
                p_y_probabilities /= M_eval / batch_size
                p_z_means /= M_eval / batch_size
                p_z_variances /= M_eval / batch_size
            
            if self.number_of_importance_samples["evaluation"] == 1 \
                and self.number_of_monte_carlo_samples["evaluation"] == 1:
                    stddev_of_p_x_given_z_mean_eval = None
            
            evaluation_cluster_ids = q_y_logits.argmax(axis = 1)
            
            if evaluation_set.has_labels:
                predicted_evaluation_label_ids = mapClusterIDsToLabelIDs(
                    evaluation_label_ids,
                    evaluation_cluster_ids,
                    excluded_class_ids
                )
                accuracy_eval = accuracy(
                    evaluation_label_ids,
                    predicted_evaluation_label_ids,
                    excluded_class_ids
                )
            else:
                accuracy_eval = None
            
            if evaluation_set.label_superset:
                predicted_evaluation_superset_label_ids = \
                    mapClusterIDsToLabelIDs(
                    evaluation_superset_label_ids,
                    evaluation_cluster_ids,
                    excluded_superset_class_ids
                )
                accuracy_superset_eval = accuracy(
                    evaluation_superset_label_ids,
                    predicted_evaluation_superset_label_ids,
                    excluded_superset_class_ids
                )
                accuracy_display = accuracy_superset_eval
            else:
                accuracy_superset_eval = None
                accuracy_display = accuracy_eval
            
            if log_results:
                
                summary = tf.Summary()
                summary.value.add(tag="losses/lower_bound",
                    simple_value = ELBO_eval)
                summary.value.add(tag="losses/reconstruction_error",
                    simple_value = ENRE_eval)
                summary.value.add(tag="losses/kl_divergence_z",
                    simple_value = KL_z_eval)
                summary.value.add(tag="losses/kl_divergence_y",
                    simple_value = KL_y_eval)
                summary.value.add(tag="accuracy", simple_value = accuracy_eval)
                if accuracy_superset_eval:
                    summary.value.add(tag="superset_accuracy",
                        simple_value = accuracy_superset_eval)
    
                for k in range(self.K):
                    summary.value.add(
                        tag="prior/cluster_{}/probability".format(k),
                        simple_value = p_y_probabilities[k]
                    )
                    summary.value.add(
                        tag="posterior/cluster_{}/probability".format(k),
                        simple_value = q_y_probabilities[k]
                    )
                    for l in range(self.latent_size):
                        summary.value.add(
                            tag="prior/cluster_{}/mean/dimension_{}".format(
                                k, l),
                            simple_value = p_z_means[k][l]
                        )
                        summary.value.add(
                            tag="posterior/cluster_{}/mean/dimension_{}"
                                .format(k, l),
                            simple_value = q_z_means[k, l]
                        )
                        summary.value.add(
                            tag="prior/cluster_{}/variance/dimension_{}"\
                                .format(k, l),
                            simple_value = p_z_variances[k][l]
                        )
                        summary.value.add(
                            tag="posterior/cluster_{}/variance/dimension_{}"\
                                .format(k, l),
                            simple_value = q_z_variances[k, l]
                        )
    
                eval_summary_writer.add_summary(summary,
                    global_step = epoch + 1)
                eval_summary_writer.flush()
            
            evaluating_duration = time() - evaluating_time_start
            
            evaluation_string = "    {} set ({}): ".format(
                evaluation_set.kind.capitalize(),
                formatDuration(evaluating_duration))
            evaluation_metrics = [
                "ELBO: {:.5g}".format(ELBO_eval),
                "ENRE: {:.5g}".format(ENRE_eval),
                "KL_z: {:.5g}".format(KL_z_eval),
                "KL_y: {:.5g}".format(KL_y_eval)
            ]
            if accuracy_display:
                evaluation_metrics.append(
                    "Acc: {:.5g}".format(accuracy_display)
                )
            evaluation_string += ", ".join(evaluation_metrics)
            evaluation_string += "."
            
            print(evaluation_string)
            
            ## Data sets
            
            output_sets = [None] * len(output_versions)
            
            if "transformed" in output_versions:
                if evaluation_set_transformed:
                    transformed_evaluation_set = DataSet(
                        evaluation_set.name,
                        values = t_eval,
                        preprocessed_values = None,
                        labels = evaluation_set.labels,
                        example_names = evaluation_set.example_names,
                        feature_names = evaluation_set.feature_names,
                        feature_selection = evaluation_set.feature_selection,
                        example_filter = evaluation_set.example_filter,
                        preprocessing_methods =
                            evaluation_set.preprocessing_methods,
                        kind = evaluation_set.kind,
                        version = "transformed"
                    )
                else:
                    transformed_evaluation_set = evaluation_set
                
                index = output_versions.index("transformed")
                output_sets[index] = transformed_evaluation_set
            
            if "reconstructed" in output_versions:
                reconstructed_evaluation_set = DataSet(
                    evaluation_set.name,
                    values = p_x_mean_eval,
                    total_standard_deviations = p_x_stddev_eval,
                    explained_standard_deviations = \
                        stddev_of_p_x_given_z_mean_eval,
                    preprocessed_values = None,
                    labels = evaluation_set.labels,
                    example_names = evaluation_set.example_names,
                    feature_names = evaluation_set.feature_names,
                    feature_selection = evaluation_set.feature_selection,
                    example_filter = evaluation_set.example_filter,
                    preprocessing_methods = evaluation_set.preprocessing_methods,
                    kind = evaluation_set.kind,
                    version = "reconstructed"
                )
                index = output_versions.index("reconstructed")
                output_sets[index] = reconstructed_evaluation_set
            
            if "latent" in output_versions:
                z_evaluation_set = DataSet(
                    evaluation_set.name,
                    values = z_mean_eval,
                    preprocessed_values = None,
                    labels = evaluation_set.labels,
                    example_names = evaluation_set.example_names,
                    feature_names = numpy.array(["z variable {}".format(
                        i + 1) for i in range(self.latent_size)]),
                    feature_selection = evaluation_set.feature_selection,
                    example_filter = evaluation_set.example_filter,
                    preprocessing_methods = evaluation_set.preprocessing_methods,
                    kind = evaluation_set.kind,
                    version = "z"
                )
            
                y_evaluation_set = DataSet(
                    evaluation_set.name,
                    values = y_mean_eval,
                    preprocessed_values = None,
                    labels = evaluation_set.labels,
                    example_names = evaluation_set.example_names,
                    feature_names = numpy.array(["y variable {}".format(
                        i + 1) for i in range(self.K)]),
                    feature_selection = evaluation_set.feature_selection,
                    example_filter = evaluation_set.example_filter,
                    preprocessing_methods = evaluation_set.preprocessing_methods,
                    kind = evaluation_set.kind,
                    version = "y"
                )
            
                latent_evaluation_sets = {
                    "z": z_evaluation_set,
                    "y": y_evaluation_set
                }
                
                index = output_versions.index("latent")
                output_sets[index] = latent_evaluation_sets
            
            if predict_labels:
                if evaluation_set.has_labels:
                    predicted_evaluation_labels = class_ids_to_class_names(
                        predicted_evaluation_label_ids)
                else:
                    predicted_evaluation_labels = None
                
                if evaluation_set.has_superset_labels:
                    predicted_evaluation_superset_labels = \
                        superset_class_ids_to_superset_class_names(
                            predicted_evaluation_superset_label_ids)
                else:
                    predicted_evaluation_superset_labels = None
                
                for output_set in output_sets:
                    if isinstance(output_set, dict):
                        for variable in output_set:
                            output_set[variable].updatePredictions(
                                predicted_cluster_ids = evaluation_cluster_ids,
                                predicted_labels = predicted_evaluation_labels,
                                predicted_superset_labels = 
                                    predicted_evaluation_superset_labels
                            )
                    else:
                        output_set.updatePredictions(
                            predicted_cluster_ids = evaluation_cluster_ids,
                            predicted_labels = predicted_evaluation_labels,
                            predicted_superset_labels = 
                                predicted_evaluation_superset_labels
                        )
            
            if len(output_sets) == 1:
                output_sets = output_sets[0]
            
            return output_sets
