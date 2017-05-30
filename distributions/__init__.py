import tensorflow as tf
from numpy import inf

from tensorflow.contrib.distributions import (
    Bernoulli,
    Poisson, NegativeBinomial,
    Normal, MultivariateNormalDiag, Multinomial,
    Categorical, Mixture
)

from tensorflow.python.ops.nn import relu, softmax
from tensorflow import sigmoid, identity

from distributions.zero_inflated import ZeroInflated
from distributions.categorized import Categorized
from distributions.pareto import Pareto
from distributions.generalised_pareto import GeneralisedPareto
from distributions.multinomial_non_permuted import NonPermutedMultinomial

distributions = {
    "gaussian": {
        "parameters": {
            "mu": {
                "support": [-inf, inf],
                "activation function": identity,
                "initial value": tf.zeros
            },
            "log_sigma": {
                "support": [-3, 3],
                "activation function": identity,
                "initial value": tf.zeros
            }
        },
        "class": lambda theta: Normal(
            loc = theta["mu"], 
            scale = tf.exp(theta["log_sigma"])
        )
    },

    "gaussian mixture": {
        "parameters": {
            "logits": {
                "support": [-inf, inf],
                "activation function": identity,
                "initial value": tf.ones
            },
            "mus": {
                "support": [-inf, inf],
                "activation function": identity,
                "initial value": tf.random_normal
            },
            "log_sigmas": {
                "support": [-3, 3],
                "activation function": identity,
                "initial value": tf.zeros
            }
        },
        "class": lambda theta: Mixture(
            cat = Categorical(logits = theta["logits"]), 
            components = [MultivariateNormalDiag(
                loc = m, scale_diag = tf.exp(s)) for m, s in 
                zip(theta["mus"], theta["log_sigmas"])]
        )
    },

    "categorical": {
        "parameters": {
            "logits": {
                "support": [-inf, inf],
                "activation function": identity
            }
        },
        "class": lambda theta: 
            Categorical(logits = theta["logits"]), 
    },

    "bernoulli": {
        "parameters": {
            "p": {
                "support": [0, 1],
                "activation function": sigmoid
            }
        },
        "class": lambda theta: Bernoulli(
            probs = theta["p"], validate_args=True
        )
    },
    
    "poisson": {
        "parameters": {
            "log_lambda": {
                "support": [-10, 10],
                "activation function": identity
            }
        },
        "class": lambda theta: Poisson(
            rate = tf.exp(theta["log_lambda"])
        )
    },

    "constrained poisson": {
        "parameters": {
            "lambda": {
                "support": [0, 1],
                "activation function": softmax
            }
        },
        "class": lambda theta, N: Poisson(
            rate = theta["lambda"] * N
        )
    },

    "pareto": {
        "parameters": {
            "log_alpha": {
                "support": [-10, 10],
                "activation function": identity
            }
        },
        "class": lambda theta: Pareto(
            alpha = tf.exp(theta["log_alpha"])
        )
    },

    "generalised pareto": {
        "parameters": {
            "xi": {
                "support": [-1e4, 1e4],
                "activation function": identity
            },
            "log_sigma": {
                "support": [-3, 3],
                "activation function": identity
            }
        },
        "class": lambda theta: GeneralisedPareto(
            xi = theta["xi"],
            sigma = tf.exp(theta["log_sigma"])
            , validate_args=True)
    },

    "multinomial": {
        "parameters": {
            "p": {
                "support": [0, 1],
                "activation function": softmax
            }
        },
        "class": lambda theta, N: NonPermutedMultinomial(
                n = N,
                p = theta["p"])
    },    

    "zero-inflated poisson": {
        "parameters": {
            "pi": {
                "support": [0, 1],
                "activation function": sigmoid
            },
            "log_lambda": {
                "support": [-10, 10],
                "activation function": identity
            }
        },
        "class": lambda theta: ZeroInflated(
            Poisson(
                rate = tf.exp(theta["log_lambda"])
            ),
            pi = theta["pi"]
        )
    },
    
    "negative binomial": {
        "parameters": {
            "p": {
                "support": [0, 1],
                "activation function": sigmoid
            },
            "log_r": {
                "support": [-10, 10],
                "activation function": identity
            }
        },
        "class": lambda theta: NegativeBinomial(
            total_count = tf.exp(theta["log_r"]),
            probs = theta["p"]
        )
    },
    
    "zero-inflated negative binomial": {
        "parameters": {
            "pi": {
                "support": [0, 1],
                "activation function": sigmoid
            },
            "p": {
                "support": [0, 1],
                "activation function": sigmoid
            },
            "log_r": {
                "support": [-10, 10],
                "activation function": identity
            }
        },
        "class": lambda theta: ZeroInflated(
            NegativeBinomial(
                total_count = tf.exp(theta["log_r"]),
                probs = theta["p"]
            ),
            pi = theta["pi"]
        )
    }
}

latent_distributions = {
    "gaussian": {
        "posterior": {
            "name": "gaussian",
            "parameters": {}
            }, 
        "prior": {
            "name": "gaussian",
            "parameters": {
                "mu": 0.0,
                "log_sigma": 0.0
            }
        }
    },
    "gaussian mixture": {
        "posterior": {
            "name": "gaussian", 
            "parameters": {}
            },
        "prior": {
            "name": "gaussian mixture",
            "parameters": {}
        }
    },
    "explicit gaussian mixture": {
        "posterior": {
            "name": "gaussian mixture", 
            "parameters": {}
            },
        "prior": {
            "name": "gaussian mixture",
            "parameters": {}
        }
    },
    "fixed gaussian mixture": {
        "posterior": {
            "name": "gaussian", 
            "parameters": {}
            },
        "prior": {
            "name": "gaussian mixture",
            "parameters": {}
        }
    }
}

model_inference_graph = {
    "explicit gaussian mixture": {
        "posteriors": {
            "q_z_given_x_y": {
                "name": "gaussian", 
                "parameters": {},
                "conditioning": ["encoder", "q_y_given_x"]
            },
            "q_y_given_x": {
                "name": "categorical",
                "parameters": {},
                "conditioning": ["encoder"]
            }
        },
        "priors": {
            "p_z_given_y": {
                "name": "gaussian",
                "parameters": {},
                "conditioning": ["decoder"]
            }
        }
    },
}
