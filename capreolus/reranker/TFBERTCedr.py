import numpy as np
import tensorflow as tf
from tensorflow.python.keras.engine import data_adapter
from transformers import TFBertModel

from profane import ConfigOption, Dependency
from capreolus.reranker import Reranker


class RBFKernel(object):
    def __init__(self, init_mu, init_sigma, postfix=0, trainable=True):
        super().__init__()
        self.mu = tf.get_variable(initializer=init_mu, trainable=trainable, name=f"mu-{postfix}")
        self.sigma = tf.get_variable(initializer=init_sigma, trainable=trainable, name=f"sigma-{postfix}")

    def prob(self, x):
        adj = x - self.mu
        return tf.math.exp(-0.5 * (adj * adj) / (self.sigma * self.sigma + 1e-6))


class CEDRKNRM(object):
    """
    A KNRM model starting from simmat matrix, used as an affix of BERT
    """
    def __init__(self, mus=None, sigmas=None, train_kernels=True, scope=None):
        """
        :param query_reps: (B, n_layers, Q, n_hidden)
        :param query_mask: (B, Q), 1 for non-pad entries and 0 for pad entries
        :param doc_reps: (B, n_layers, D, n_hidden)
        :param doc_mask: (B, D), 1 for non-pad entries and 0 for pad entries
        :param mus: a list of means for kernal
        :param sigmas: a list of variance for kernal
        mus and sigmas should have same size
        """
        if not mus:
            mus = np.concatenate([np.linspace(-0.9, 0.9, 10), np.array([1.])]).astype(np.float32)
        if not sigmas:
            sigmas = np.array([0.1] * 10 + [0.001], dtype=np.float32)

        with tf.variable_scope("knrm", scope, reuse=tf.AUTO_REUSE):
            self.kernels = [
                RBFKernel(init_mu=mu, init_sigma=sigma, trainable=train_kernels, postfix=i)
                for i, (mu, sigma) in enumerate(zip(mus, sigmas))]

    def call(self, query_reps, query_mask, doc_reps, doc_mask):
        query_mask, doc_mask = tf.cast(query_mask, dtype=tf.float32), tf.cast(doc_mask, dtype=tf.float32)
        masks = tf.expand_dims(query_mask, 2) * tf.expand_dims(doc_mask, 1)  # (B, Q, D)
        masks = tf.expand_dims(masks, 1)  # (B, 1, Q, D)
        simmats_dot = tf.linalg.matmul(query_reps, doc_reps, transpose_b=True)  # (B, n_layer, Q, D)
        query_norm = tf.expand_dims(tf.norm(query_reps, axis=-1), -1)  # (B, n_layers, Q, 1)
        doc_norm = tf.expand_dims(tf.norm(doc_reps, axis=-1), -2)  # (B, n_layers, 1, D)
        simmats = tf.math.divide(simmats_dot, (tf.multiply(query_norm, doc_norm) + 1e-6)) * masks

        query_mask = tf.expand_dims(query_mask, 1)  # (B, 1, Q)
        kernel_features = [tf.reduce_sum(masks * k.prob(simmats), -1) for k in self.kernels]  # (B, n_layer, Q) * K
        kernel_features = [query_mask * tf.log(1e-6 + kf) for kf in kernel_features]  # (B, n_layer, Q) * K
        kernel_features = [tf.reduce_sum(kf, -1) for kf in kernel_features]  # (B, n_layer) * K
        kernel_features = tf.concat(kernel_features, axis=-1)  # (B, n_layer * K)
        return kernel_features


class TFBERTCedr_Class(tf.keras.layers.Layer):
    def __init__(self, extractor, config, *args, **kwargs):
        super(TFBERTCedr_Class, self).__init__(*args, **kwargs)
        self.extractor = extractor
        self.config = config
        self.bert = TFBertModel.from_pretrained(config["pretrain"], hidden_dropout_prob=0.1)
        self.dropout = tf.keras.layers.Dropout(config.hidden_dropout_prob)
        self.classifier = tf.keras.layers.Dense(
            config.num_labels,
            kernel_initializer=tf.keras.initializers.TruncatedNormal(stddev=0.02),
            name="classifier"
        )
        self.nir = CEDRKNRM(train_kernels=config["knrm_trainkernel"])

    def call(self, x, **kwargs):
        """ Returns logits of shape [2] """
        doc_bert_input, doc_mask, doc_seg = x[0], x[1], x[2]
        outputs = self.bert(doc_bert_input, attention_mask=doc_mask, token_type_ids=doc_seg)
        last_layer_output, pooled_output = outputs[0], outputs[1]  # (B, H)

        # nir_output = self.nir.call(last_layer_output)  # (B, T, H) -> (B, K)
        pooled_output = self.dropout(pooled_output, training=kwargs.get("training", False))

        # cedr_output = tf.concat([pooled_output, nir_output], axis=-1)
        cedr_output = pooled_output
        logits = self.classifier(cedr_output)  # (B, config.num_labels)
        return logits

    def predict_step(self, data):
        """
        Scores each passage and applies max pooling over it.
        """
        # prepare input
        posdoc_bert_input, posdoc_mask, posdoc_seg, negdoc_bert_input, negdoc_mask, negdoc_seg = data
        batch_size = tf.shape(posdoc_bert_input)[0]
        num_passages = self.extractor.config["numpassages"]
        maxseqlen = self.extractor.config["maxseqlen"]

        posdoc_bert_input = tf.reshape(posdoc_bert_input, [batch_size * num_passages, maxseqlen])
        posdoc_mask = tf.reshape(posdoc_mask, [batch_size * num_passages, maxseqlen])
        posdoc_seg = tf.reshape(posdoc_seg, [batch_size * num_passages, maxseqlen])

        # feed input to model
        passage_scores = self.call((posdoc_bert_input, posdoc_mask, posdoc_seg), training=False)[:, 1]
        tf.debugging.assert_equal(tf.shape(passage_scores), (batch_size * num_passages))
        passage_scores = tf.reshape(passage_scores, [batch_size, num_passages])
        passage_scores = tf.math.reduce_max(passage_scores, axis=1)

        return passage_scores

    def score(self, x, **kwargs):
        posdoc_bert_input, posdoc_mask, posdoc_seg, negdoc_bert_input, negdoc_mask, negdoc_seg = x

        return self.call((posdoc_bert_input, posdoc_mask, posdoc_seg), **kwargs)

    def score_pair(self, x, **kwargs):
        posdoc_bert_input, posdoc_mask, posdoc_seg, negdoc_bert_input, negdoc_mask, negdoc_seg = x

        pos_score = self.call((posdoc_bert_input, posdoc_mask, posdoc_seg), **kwargs)[:, 1]
        neg_score = self.call((negdoc_bert_input, negdoc_mask, negdoc_seg), **kwargs)[:, 1]

        return pos_score, neg_score


# @Reranker.register
class TFBERTCedr(Reranker):
    module_name = "TFBERTCedr"

    dependencies = [
        Dependency(key="extractor", module="extractor", name="bertpassage"),
        Dependency(key="trainer", module="trainer", name="tensorflow"),
    ]

    config_spec = [
        ConfigOption("pretrained", "bert-base-uncased", "Hugging face transformer pretrained model"),
        ConfigOption("modeltype", "vbert", "which type of bert model to run. Options: vbert, nir, cedr"),
        ConfigOption("nirmodel", "KRNM", "which Neural IR model to to integrate with bert. Options: KNRM, DRMM, PACRR"),

        # for knrm:
        ConfigOption(f"knrm_trainkernel", True, "Whether to train KNRM kernel.")
    ]

    def build_model(self):
        self.model = TFBERTCedr_Class(self.extractor, self.config)
        return self.model
