import os
import pickle

import capnp
from tqdm import tqdm
import numpy as np

from capreolus.tokenizer import Tokenizer
from capreolus.utils.common import padlist
from capreolus.utils.loginit import get_logger

logger = get_logger(__name__)


class Extractor:
    """ Module responsible for transforming raw query and document text into inputs suitable for a reranker. """

    def __init__(self, cache_path, feature_cache_dir, pipeline_config, benchmark=None, collection=None, index=None):
        """
        :param index: We need an index so as to calculate IDF
        """
        self.pipeline_config = pipeline_config
        self.cache_path = cache_path
        self.feature_cache_dir = feature_cache_dir
        self.benchmark = benchmark
        self.collection = collection
        self.index = index
        self.embeddings = None

    def transform_qid_posdocid_negdocid(self, q_id, posdoc_id, negdoc_id=None):
        raise NotImplementedError

    def build_from_benchmark(self, *args, **kwargs):
        raise NotImplementedError

    def build_benchmark_or_use_cached(self, *args, **kwargs):
        cache_key = str(sorted([(key, value) for key, value in self.pipeline_config.items()]))
        cache_file = "{0}/{1}.cache".format(self.feature_cache_dir, cache_key)
        if os.path.isfile(cache_file):
            return pickle.load(open(cache_file, "rb"))
        else:
            self.build_from_benchmark(*args, **kwargs)
            pickle.dump(self, open(cache_file, "wb"), protocol=2)
            return self

class BuildStoIMixin:
    def build_stoi(self, toks_list, keepstops, calculate_idf):
        assert self.index is not None
        assert self.stoi is not None
        assert self.idf is not None

        for toks in tqdm(toks_list, unit_scale=True):
            for tok in toks:
                if tok not in self.stoi:
                    self.stoi[tok] = len(self.stoi)

                    if calculate_idf:
                        # TODO: This is a temp hack. Refactor so that we query from an index where stop words are kept
                        self.idf[tok] = self.index.getidf(tok)
