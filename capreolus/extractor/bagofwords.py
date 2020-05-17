import pickle
import os
import time

from capreolus.extractor import Extractor
from capreolus.tokenizer import Tokenizer
from capreolus.utils.loginit import get_logger
from tqdm import tqdm
import numpy as np
from collections import Counter, defaultdict

from capreolus.registry import Dependency

logger = get_logger(__name__)  # pylint: disable=invalid-name


class BagOfWords(Extractor):
    """ Bag of Words (or bag of trigrams when `datamode=trigram`) extractor. Used with the DSSM reranker. """

    name = "bagofwords"
    dependencies = {
        "index": Dependency(module="index", name="anserini", config_overrides={"indexstops": True, "stemmer": "none"}),
        "tokenizer": Dependency(module="tokenizer", name="anserini"),
    }
    pad = 0
    pad_tok = "<pad>"

    @staticmethod
    def config():
        datamode = "unigram"  # type of input: 'unigram' or 'trigram'
        maxqlen = 4
        maxdoclen = 800
        usecache = False

    def _tok2vec(self, toks):
        # return [self.embeddings[self.stoi[tok]] for tok in toks]
        return [self.stoi.get(tok, 0) for tok in toks]

    def load_state(self, qids, docids):
        with open(self.get_state_cache_file_path(qids, docids), "rb") as f:
            state_dict = pickle.load(f)
            self.qid2toks = state_dict["qid2toks"]
            self.docid2toks = state_dict["docid2toks"]
            self.stoi = state_dict["stoi"]
            self.itos = state_dict["itos"]
            self.idf = defaultdict(lambda: 0, state_dict["idf"])

    def cache_state(self, qids, docids):
        os.makedirs(self.get_cache_path(), exist_ok=True)
        with open(self.get_state_cache_file_path(qids, docids), "wb") as f:
            state_dict = {
                "qid2toks": self.qid2toks,
                "docid2toks": self.docid2toks,
                "stoi": self.stoi,
                "itos": self.itos,
                "idf": dict(self.idf),
            }
            pickle.dump(state_dict, f, protocol=-1)

    def get_trigrams_for_toks(self, toks_list):
        return [("#%s#" % tok)[i : i + 3] for tok in toks_list for i in range(len(tok))]

    def _build_vocab_unigram(self, qids, docids, topics):
        tokenize = self["tokenizer"].tokenize
        self.qid2toks = {qid: tokenize(topics[qid]) for qid in qids}
        self.docid2toks = {docid: tokenize(self["index"].get_doc(docid)) for docid in docids}
        self._extend_stoi(self.qid2toks.values(), calc_idf=True)
        self._extend_stoi(self.docid2toks.values())
        self.itos = {i: s for s, i in self.stoi.items()}
        logger.info(f"vocabulary constructed, with {len(self.itos)} terms in total")

    def _build_vocab_trigram(self, qids, docids, topics):
        tokenize = self["tokenizer"].tokenize
        self.qid2toks = {qid: self.get_trigrams_for_toks(tokenize(topics[qid])) for qid in qids}
        self.docid2toks = {docid: self.get_trigrams_for_toks(tokenize(self["index"].get_doc(docid))) for docid in docids}
        self._extend_stoi(self.qid2toks.values(), calc_idf=True)
        self._extend_stoi(self.docid2toks.values())
        self.itos = {i: s for s, i in self.stoi.items()}
        logger.info(f"vocabulary constructed, with {len(self.itos)} terms in total")

    def _build_vocab(self, qids, docids, topics):
        if self.is_state_cached(qids, docids) and self.cfg["usecache"]:
            self.load_state(qids, docids)
            logger.info("Vocabulary loaded from cache")
        else:
            if self.cfg["datamode"] == "unigram":
                self._build_vocab_unigram(qids, docids, topics)
            elif self.cfg["datamode"] == "trigram":
                self._build_vocab_trigram(qids, docids, topics)
            else:
                raise NotImplementedError
            if self.cfg["usecache"]:
                self.cache_state(qids, docids)

        self.embeddings = self.stoi

    def exist(self):
        return hasattr(self, "qid2toks") and hasattr(self, "docid2toks") and len(self.stoi) > 1

    def create(self, qids, docids, topics):
        if self.exist():
            return
        self["index"].create_index()
        self.itos = {self.pad: self.pad_tok}
        self.stoi = {self.pad_tok: self.pad}
        self.qid2toks = defaultdict(list)
        self.docid2toks = defaultdict(list)
        self.idf = defaultdict(lambda: 0)
        self.embeddings = None
        # self.cache = self.load_cache()    # TODO

        self._build_vocab(qids, docids, topics)

    def id2vec(self, posdoc_id, negdoc_id=None, qid=None, query=None):
        # TODO: Get rid of this if check. Standardize the interface
        if query is not None:
            if qid is None:
                query_toks = self["tokenizer"].tokenize(query)
            else:
                raise RuntimeError("received both a qid and query, but only one can be passed")

        else:
            query_toks = self.qid2toks[qid]
        posdoc_toks = self.docid2toks.get(posdoc_id)
        if not posdoc_toks:
            logger.debug("missing docid %s", posdoc_id)
            return None

        transformed_query = self.transform_txt(query_toks, self.cfg["maxqlen"])

        query_idf_vector = np.zeros(len(self.stoi), dtype=np.float32)
        for tok in query_toks:
            query_idf_vector[self.stoi.get(tok, 0)] = self.idf[tok]

        transformed = {
            "qid": qid,
            "posdocid": posdoc_id,
            "query": transformed_query,
            "posdoc": self.transform_txt(posdoc_toks, self.cfg["maxdoclen"]),
            "query_idf": query_idf_vector,
        }
        if negdoc_id is not None:
            negdoc_toks = self.docid2toks.get(negdoc_id)
            if not negdoc_toks:
                logger.debug("missing docid %s", negdoc_id)
                return None
            transformed["negdocid"] = negdoc_id
            transformed["negdoc"] = self.transform_txt(negdoc_toks, self.cfg["maxdoclen"])

        return transformed

    def transform_txt(self, term_list, maxlen):
        term_vec = self._tok2vec(term_list)
        nvocab = len(self.stoi)
        bog_txt = np.zeros(nvocab, dtype=np.float32)

        if self.cfg["datamode"] == "unigram":
            for term in term_vec:
                bog_txt[term] += 1
        elif self.cfg["datamode"] == "trigram":
            trigrams = self.get_trigrams_for_toks(term_list)
            toks = [self.stoi.get(trigram, 0) for trigram in trigrams]
            tok_counts = Counter(toks)
            for tok, count in tok_counts.items():
                bog_txt[tok] = count
        else:
            raise Exception("Unknown datamode")

        return bog_txt