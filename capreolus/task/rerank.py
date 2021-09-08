import os
from collections import defaultdict
from pathlib import Path

from ir_measures import *
from ir_measures.measures import Measure

from capreolus import ConfigOption, Dependency, evaluator
from capreolus.sampler import PredSampler
from capreolus.searcher import Searcher
from capreolus.evaluator import log_metrics_verbose, format_metrics_string
from capreolus.task import Task
from capreolus.utils.trec import DEFAULT_METRICS
from capreolus.utils.loginit import get_logger

logger = get_logger(__name__)


@Task.register
class RerankTask(Task):
    module_name = "rerank"
    config_spec = [
        ConfigOption("fold", "s1", "fold to run"),
        ConfigOption("optimize", "AP", "metric to maximize on the dev set"),  # affects train() because we check to save weights
        ConfigOption("metrics", "default", "metrics reported for evaluation", value_type="strlist"),
        ConfigOption("threshold", 100, "Number of docids per query to evaluate during prediction"),
        ConfigOption("testthreshold", 1000, "Number of docids per query to evaluate on test data"),
    ]
    dependencies = [
        Dependency(
            key="benchmark", module="benchmark", name="robust04.yang19", provide_this=True, provide_children=["collection"]
        ),
        Dependency(key="rank", module="task", name="rank"),
        Dependency(key="reranker", module="reranker", name="KNRM"),
        Dependency(key="sampler", module="sampler", name="triplet"),
    ]

    commands = ["train", "evaluate", "traineval"] + Task.help_commands
    default_command = "describe"

    def traineval(self):
        self.train()
        self.evaluate()

    def train(self):
        fold = self.config["fold"]

        self.rank.search()
        rank_results = self.rank.evaluate()
        best_search_run_path = rank_results["path"][fold]
        best_search_run = Searcher.load_trec_run(best_search_run_path)

        return self.rerank_run(best_search_run, self.get_results_path())

    def rerank_run(self, best_search_run, train_output_path, include_train=False):
        if not isinstance(train_output_path, Path):
            train_output_path = Path(train_output_path)

        fold = self.config["fold"]
        optimize = self.config["optimize"] if isinstance(self.config["optimize"], Measure) else eval(self.config["optimize"])
        dev_output_path = train_output_path / "pred" / "dev"
        logger.debug("results path: %s", train_output_path)

        docids = set(docid for querydocs in best_search_run.values() for docid in querydocs)
        self.reranker.extractor.preprocess(
            qids=best_search_run.keys(), docids=docids, topics=self.benchmark.topics[self.benchmark.query_type]
        )
        self.reranker.build_model()
        self.reranker.searcher_scores = best_search_run

        train_run = {qid: docs for qid, docs in best_search_run.items() if qid in self.benchmark.folds[fold]["train_qids"]}
        # For each qid, select the top 100 (defined by config["threshold") docs to be used in validation
        dev_run = defaultdict(dict)
        # This is possible because best_search_run is an OrderedDict
        for qid, docs in best_search_run.items():
            if qid in self.benchmark.folds[fold]["predict"]["dev"]:
                for idx, (docid, score) in enumerate(docs.items()):
                    if idx >= self.config["threshold"]:
                        break
                    dev_run[qid][docid] = score

        # Depending on the sampler chosen, the dataset may generate triplets or pairs
        train_dataset = self.sampler
        train_dataset.prepare(
            train_run, self.benchmark.qrels, self.reranker.extractor, relevance_level=self.benchmark.relevance_level
        )
        dev_dataset = PredSampler()
        dev_dataset.prepare(
            dev_run, self.benchmark.qrels, self.reranker.extractor, relevance_level=self.benchmark.relevance_level
        )

        dev_qrels = {qid: self.benchmark.qrels[qid] for qid in self.benchmark.non_nn_dev[fold] if qid in self.benchmark.qrels}
        dev_preds = self.reranker.trainer.train(
            self.reranker,
            train_dataset,
            train_output_path,
            dev_dataset,
            dev_output_path,
            dev_qrels,
            optimize,
            self.benchmark,
        )

        self.reranker.trainer.load_best_model(self.reranker, train_output_path)
        dev_output_path = train_output_path / "pred" / "dev" / "best"
        if not dev_output_path.exists():
            dev_preds = self.reranker.trainer.predict(self.reranker, dev_dataset, dev_output_path)

        test_run = defaultdict(dict)
        # This is possible because best_search_run is an OrderedDict
        for qid, docs in best_search_run.items():
            if qid in self.benchmark.folds[fold]["predict"]["test"]:
                for idx, (docid, score) in enumerate(docs.items()):
                    if idx >= self.config["testthreshold"]:
                        break
                    test_run[qid][docid] = score

        test_dataset = PredSampler()
        test_dataset.prepare(
            test_run, self.benchmark.qrels, self.reranker.extractor, relevance_level=self.benchmark.relevance_level
        )
        test_output_path = train_output_path / "pred" / "test" / "best"
        test_preds = self.reranker.trainer.predict(self.reranker, test_dataset, test_output_path)

        preds = {"dev": dev_preds, "test": test_preds}

        if include_train:
            train_dataset = PredSampler(
                train_run, self.benchmark.qrels, self.reranker.extractor, relevance_level=self.benchmark.relevance_level
            )

            train_output_path = train_output_path / "pred" / "train" / "best"
            train_preds = self.reranker.trainer.predict(self.reranker, train_dataset, train_output_path)
            preds["train"] = train_preds

        return preds

    def predict(self):
        fold = self.config["fold"]
        self.rank.search()
        rank_results = self.rank.evaluate()
        best_search_run_path = rank_results["path"][fold]
        best_search_run = Searcher.load_trec_run(best_search_run_path)

        docids = set(docid for querydocs in best_search_run.values() for docid in querydocs)
        self.reranker.extractor.preprocess(
            qids=best_search_run.keys(), docids=docids, topics=self.benchmark.topics[self.benchmark.query_type]
        )
        train_output_path = self.get_results_path()
        self.reranker.build_model()
        self.reranker.trainer.load_best_model(self.reranker, train_output_path)

        test_run = defaultdict(dict)
        # This is possible because best_search_run is an OrderedDict
        for qid, docs in best_search_run.items():
            if qid in self.benchmark.folds[fold]["predict"]["test"]:
                for idx, (docid, score) in enumerate(docs.items()):
                    if idx >= self.config["testthreshold"]:
                        break
                    test_run[qid][docid] = score

        test_dataset = PredSampler()
        test_dataset.prepare(
            test_run, self.benchmark.qrels, self.reranker.extractor, relevance_level=self.benchmark.relevance_level
        )
        test_output_path = train_output_path / "pred" / "test" / "best"
        test_preds = self.reranker.trainer.predict(self.reranker, test_dataset, test_output_path)

        preds = {"test": test_preds}

        return preds

    def bircheval(self):
        fold = self.config["fold"]
        train_output_path = self.get_results_path()
        searcher_runs, reranker_runs = self.find_birch_crossvalidated_results()

        fold_test_metrics = self.benchmark.evaluate(reranker_runs[fold]["test"], metrics=DEFAULT_METRICS)
        logger.info("rerank: fold=%s test metrics: %s", fold, fold_test_metrics)

    def evaluate(self):
        fold = self.config["fold"]
        train_output_path = self.get_results_path()
        logger.debug("results path: %s", train_output_path)
        metrics = self.config["metrics"] if list(self.config["metrics"]) != ["default"] else DEFAULT_METRICS
        metrics = list(map(convert_metric, metrics))
        optimize = convert_metric(self.config["optimize"])

        searcher_runs, reranker_runs = self.find_crossvalidated_results()

        if fold not in reranker_runs:
            logger.error("could not find predictions; run the train command first")
            raise ValueError("could not find predictions; run the train command first")

        dev_qrels = {qid: self.benchmark.qrels.get(qid, {}) for qid in self.benchmark.folds[fold]["predict"]["dev"]}
        fold_dev_metrics = self.benchmark.evaluate(reranker_runs[fold]["dev"], dev_qrels, metrics)
        pretty_fold_dev_metrics = format_metrics_string(fold_dev_metrics)
        logger.info("rerank: fold=%s dev metrics: %s", fold, pretty_fold_dev_metrics)

        test_qrels = {qid: self.benchmark.qrels.get(qid, {}) for qid in self.benchmark.folds[fold]["predict"]["test"]}
        fold_test_metrics = self.benchmark.evaluate(reranker_runs[fold]["test"], test_qrels, metrics)
        pretty_fold_test_metrics = format_metrics_string(fold_test_metrics)
        logger.info("rerank: fold=%s test metrics: %s", fold, pretty_fold_test_metrics)

        if len(reranker_runs) != len(self.benchmark.folds):
            logger.info(
                "rerank: skipping cross-validated metrics because results exist for only %s/%s folds",
                len(reranker_runs),
                len(self.benchmark.folds),
            )
            return {
                "fold_test_metrics": fold_test_metrics,
                "fold_dev_metrics": fold_dev_metrics,
                "cv_metrics": None,
                "interpolated_cv_metrics": None,
            }

        logger.info("rerank: average cross-validated metrics when choosing iteration based on '%s':", self.config["optimize"])
        all_preds = {}
        for preds in reranker_runs.values():
            for qid, docscores in preds["test"].items():
                all_preds.setdefault(qid, {})
                for docid, score in docscores.items():
                    all_preds[qid][docid] = score

        cv_metrics = self.benchmark.evaluate(all_preds, metrics=metrics)
        interpolated_results = evaluator.interpolated_eval(searcher_runs, reranker_runs, self.benchmark, optimize, metrics)

        log_metrics_verbose(cv_metrics)
        log_metrics_verbose(interpolated_results["score"])

        return {
            "fold_test_metrics": fold_test_metrics,
            "fold_dev_metrics": fold_dev_metrics,
            "cv_metrics": cv_metrics,
            "interpolated_results": interpolated_results,
        }

    def find_crossvalidated_results(self):
        searcher_runs = {}
        rank_results = self.rank.evaluate()
        for fold in self.benchmark.folds:
            searcher_runs[fold] = {"dev": Searcher.load_trec_run(rank_results["path"][fold])}
            searcher_runs[fold]["test"] = searcher_runs[fold]["dev"]

        reranker_runs = {}
        train_output_path = self.get_results_path()
        test_output_path = train_output_path / "pred" / "test" / "best"
        dev_output_path = train_output_path / "pred" / "dev" / "best"
        for fold in self.benchmark.folds:
            # TODO fix by using multiple Tasks
            test_path = Path(test_output_path.as_posix().replace("fold-" + self.config["fold"], "fold-" + fold))
            if os.path.exists(test_path):
                reranker_runs.setdefault(fold, {})["test"] = Searcher.load_trec_run(test_path)

                dev_path = Path(dev_output_path.as_posix().replace("fold-" + self.config["fold"], "fold-" + fold))
                reranker_runs.setdefault(fold, {})["dev"] = Searcher.load_trec_run(dev_path)

        return searcher_runs, reranker_runs

    def find_birch_crossvalidated_results(self):
        searcher_runs = {}
        rank_results = self.rank.evaluate()
        reranker_runs = {}
        train_output_path = self.get_results_path()
        test_output_path = train_output_path / "pred" / "test" / "best"
        dev_output_path = train_output_path / "pred" / "dev" / "best"
        for fold in self.benchmark.folds:
            # TODO fix by using multiple Tasks
            test_path = Path(test_output_path.as_posix().replace("fold-" + self.config["fold"], "fold-" + fold))
            if os.path.exists(test_path):
                reranker_runs.setdefault(fold, {})["test"] = Searcher.load_trec_run(test_path)

        return searcher_runs, reranker_runs
