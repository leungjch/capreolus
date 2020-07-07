import gzip
import json
import pickle
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile

from tqdm import tqdm

from capreolus import ConfigOption, Dependency, constants
from capreolus.utils.common import download_file, remove_newline
from capreolus.utils.loginit import get_logger
from capreolus.utils.trec import topic_to_trectxt

from . import Benchmark

logger = get_logger(__name__)
PACKAGE_PATH = constants["PACKAGE_PATH"]


@Benchmark.register
class CodeSearchNetCorpus(Benchmark):
    """CodeSearchNet Corpus. [1]

       [1] Hamel Husain, Ho-Hsiang Wu, Tiferet Gazit, Miltiadis Allamanis, and Marc Brockschmidt. 2019. CodeSearchNet Challenge: Evaluating the State of Semantic Code Search. arXiv 2019.
    """

    module_name = "codesearchnet_corpus"
    dependencies = [Dependency(key="collection", module="collection", name="codesearchnet")]
    query_type = "title"
    file_fn = PACKAGE_PATH / "data" / "csn_corpus"

    @property
    def query_map(self):
        if not hasattr(self, "_query_map"):
            self.download_if_missing()

        return self._query_map

    def get_qid(self, query):
        return self.query_map.get(query, -1)

    def get_docid(self, url, doc):
        return self.collection.get_docid(url, doc)

    def build(self):
        config = self.collection.config
        lang = config["lang"]

        self.query_map_file = self.file_fn / lang / "querymap.json"
        self.qrel_file = self.file_fn / lang / "qrels.txt"
        self.fold_file = self.file_fn / lang / "fold.json"
        self.topic_file = self.get_cache_path() / "topic.txt"

        for file in [var for var in vars(self) if var.endswith("file")]:
            getattr(self, file).parent.mkdir(exist_ok=True, parents=True)

        self.download_if_missing()

    def generate_parsed_doc_from_gz(self, dir):
        """ generate parsed dict-format doc from all jsonl.gz files under given directory """
        config = self.collection.config
        for fn in sorted(dir.glob("*.jsonl.gz")):
            f = gzip.open(fn, "rb")
            for data in f:
                data = json.loads(data)
                codes = self.collection.process_text(
                    sent=" ".join(data["code_tokens"]),
                    lang=config["lang"],
                    remove_keywords=config["removekeywords"],
                    tokenize_code=config["tokenizecode"],
                    remove_unichar=config["removeunichar"])
                docstrings = self.collection.process_text(
                    sent=" ".join(data["docstring_tokens"]),
                    lang=config["lang"],
                    remove_keywords=False,
                    tokenize_code=config["tokenizecode"],
                    remove_unichar=config["removeunichar"])
                yield {
                    "url": data["url"],
                    "code_raw": codes["raw"],
                    "code_final": codes["final"],
                    "docstring_raw": docstrings["raw"],
                    "docstring_final": docstrings["final"]
                }

    def download_if_missing(self):
        files = [self.query_map_file, self.qrel_file, self.topic_file, self.fold_file]
        if all([f.exists() for f in files]):
            self._query_map = json.load(open(self.query_map_file, "r"))
            return

        lang = self.collection.config["lang"]
        raw_dir = self.collection.download_raw()

        # prepare folds, qrels, topics, docstring2qid  # TODO: shall we add negative samples?
        qrels, self._query_map = defaultdict(dict), {}
        qids = {s: [] for s in ["train", "valid", "test"]}

        topic_file = open(self.topic_file, "w", encoding="utf-8")
        qrel_file = open(self.qrel_file, "w", encoding="utf-8")

        for set_name in qids:
            set_path = raw_dir / lang / "final" / "jsonl" / set_name
            for data in self.generate_parsed_doc_from_gz(set_path):
                n_words_in_docstring = len(data["docstring_final"].split())
                if n_words_in_docstring >= 1024:
                    logger.warning(
                        f"chunk query to first 1000 words otherwise TooManyClause would be triggered "
                        f"at lucene at search stage, ")
                    data["docstring_final"] = " ".join(data["docstring_final"].split()[:1020])  # for TooManyClause Exception

                docid = self.collection.get_docid(data["url"], data["code_raw"])
                qid = self._query_map.get(data["code_raw"], str(len(self._query_map)))
                qrel_file.write(f"{qid} Q0 {docid} 1\n")

                if data["docstring_raw"] not in self._query_map:
                    self._query_map[data["docstring_raw"]] = qid
                    qids[set_name].append(qid)
                    topic_file.write(topic_to_trectxt(qid, data["docstring_final"]))

        topic_file.close()
        qrel_file.close()

        # write to qid_map.json, docid_map, fold.json
        json.dump(self._query_map, open(self.query_map_file, "w"))
        json.dump(
            {"s1": {"train_qids": qids["train"], "predict": {"dev": qids["valid"], "test": qids["test"]}}},
            open(self.fold_file, "w"),
        )

        assert all([f.exists() for f in files])


@Benchmark.register
class CodeSearchNetChallenge(Benchmark):
    """CodeSearchNet Challenge. [1]
       This benchmark can only be used for training (and challenge submissions) because no qrels are provided.

       [1] Hamel Husain, Ho-Hsiang Wu, Tiferet Gazit, Miltiadis Allamanis, and Marc Brockschmidt. 2019. CodeSearchNet Challenge: Evaluating the State of Semantic Code Search. arXiv 2019.
    """

    module_name = "codesearchnet_challenge"
    dependencies = [Dependency(key="collection", module="collection", name="codesearchnet")]
    config_spec = [ConfigOption("lang", "ruby", "CSN language dataset to use")]

    url = "https://raw.githubusercontent.com/github/CodeSearchNet/master/resources/queries.csv"
    query_type = "title"

    file_fn = PACKAGE_PATH / "data" / "csn_challenge"
    topic_file = file_fn / "topics.txt"
    qid_map_file = file_fn / "qidmap.json"

    def download_if_missing(self):
        """ download query.csv and prepare queryid - query mapping file """
        if self.topic_file.exists() and self.qid_map_file.exists():
            return

        tmp_dir = Path("/tmp")
        tmp_dir.mkdir(exist_ok=True, parents=True)
        self.file_fn.mkdir(exist_ok=True, parents=True)

        query_fn = tmp_dir / f"query.csv"
        if not query_fn.exists():
            download_file(self.url, query_fn)

        # prepare qid - query
        qid_map = {}
        topic_file = open(self.topic_file, "w", encoding="utf-8")
        query_file = open(query_fn)
        for qid, line in enumerate(query_file):
            if qid != 0:  # ignore the first line "query"
                topic_file.write(topic_to_trectxt(qid, line.strip()))
                qid_map[qid] = line
        topic_file.close()
        json.dump(qid_map, open(self.qid_map_file, "w"))