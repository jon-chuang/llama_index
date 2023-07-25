import re
from shutil import rmtree
import string
from typing import Any, List, Dict, Optional, Tuple
from llama_index.indices.query.base import BaseQueryEngine
from llama_index.indices.query.schema import QueryBundle
from llama_index.query_engine.retriever_query_engine import RetrieverQueryEngine
from llama_index.query_engine.multistep_query_engine import MultiStepQueryEngine
from llama_index.utils import get_cache_dir
from llama_index.schema import NodeWithScore, TextNode
from llama_index.indices.base_retriever import BaseRetriever
import os
import tqdm
import requests
import json
from collections import Counter
from copy import copy

URL = """http://curtis.ml.cmu.edu/datasets/\
hotpot/hotpot_{dataset}_v1.json"""


class HotpotQAEvaluator:
    """
    Refer to https://hotpotqa.github.io/ for more details on the dataset
    """

    def _download_datasets(self, datasets: List[str]) -> Dict[str, str]:
        cache_dir = get_cache_dir()

        dataset_paths = {}
        for dataset in datasets:
            dataset_full_path = os.path.join(
                cache_dir, "datasets", "HotpotQA", dataset + ".json"
            )
            if not os.path.exists(dataset_full_path):
                url = URL.format(dataset=dataset)

                try:
                    os.makedirs(os.path.dirname(dataset_full_path), exist_ok=True)
                    save_file = open(dataset_full_path, "wb")
                    response = requests.get(url, stream=True)

                    # Define the size of each chunk
                    chunk_size = 1024

                    # Loop over the chunks and parse the JSON data
                    print("Downloading dataset:", dataset)
                    for chunk in tqdm.tqdm(
                        response.iter_content(chunk_size=chunk_size), total=chunk_size
                    ):
                        if chunk:
                            save_file.write(chunk)
                except Exception as e:
                    if os.path.exists(dataset_full_path):
                        print(
                            "Dataset:",
                            dataset,
                            "not found at:",
                            url,
                            "Removing cached dir",
                        )
                        rmtree(dataset_full_path)
                    raise ValueError(f"could not download {dataset} dataset") from e
            dataset_paths[dataset] = dataset_full_path
            print("Dataset:", dataset, "downloaded at:", dataset_full_path)
        return dataset_paths

    def run(
        self,
        query_engine: BaseQueryEngine,
        queries: int = 10,
        queries_fraction: Optional[float] = None,
        show_result: bool = False,
        datasets: List[str] = ["dev_distractor"],
    ) -> None:
        dataset_paths = self._download_datasets(datasets)
        for dataset, path in zip(datasets, dataset_paths):
            dataset_path = dataset_paths[dataset]
            print("Evaluating on dataset:", dataset)
            print("-------------------------------------")

            f = open(dataset_path)
            query_objects = json.loads(f.read())
            if queries_fraction:
                queries_to_load = int(len(query_objects) * queries_fraction)
            else:
                queries_to_load = queries
                queries_fraction = round(queries / len(query_objects), 5)

            print(
                f"Loading {queries_to_load} queries out of \
{len(query_objects)} (fraction: {queries_fraction})"
            )
            query_objects = query_objects[:queries_to_load]

            if dataset == "dev_distractor":
                assert isinstance(
                    query_engine, RetrieverQueryEngine
                ), "Query engine must be a retriever query engine for this dataset"
                retriever = HotpotQARetriever(query_objects)  # type: ignore
            elif dataset == "dev_fullwiki":
                retriever = ColbertV2WikipediaRetriever()  # type: ignore
            else:
                raise ValueError(f"Dataset {dataset} is not supported")

            # Mock the query engine's retriever
            query_engine = replace_retriever(query_engine, retriever)

            scores = {"exact_match": 0.0, "f1": 0.0}

            for query in query_objects:
                response = query_engine.query(query["question"])
                em = int(
                    exact_match_score(
                        prediction=str(response), ground_truth=query["answer"]
                    )
                )
                f1, _, _ = f1_score(
                    prediction=str(response), ground_truth=query["answer"]
                )
                scores["exact_match"] += em
                scores["f1"] += f1
                if show_result:
                    print("Question: ", query["question"])
                    print("Response:", response)
                    # print("Sources: ", response.get_formatted_sources())
                    print("Correct answer: ", query["answer"])
                    print("EM:", em, "F1:", f1)
                    print("-------------------------------------")

            for score in scores:
                scores[score] /= len(query_objects)

            print("Scores: ", scores)


class HotpotQARetriever(BaseRetriever):
    """
    This is a mocked retriever for HotpotQA dataset. It is only meant to be used
    with the hotpotqa dev dataset in the distractor setting. This is the setting that
    does not require retrieval but requires identifying the supporting facts from
    a list of 10 sources.
    """

    def __init__(self, query_objects: Any) -> None:
        assert isinstance(
            query_objects,
            list,
        ), f"query_objects must be a list, got: {type(query_objects)}"
        self._queries = {}
        for object in query_objects:
            self._queries[object["question"]] = object

    def _retrieve(self, query: QueryBundle) -> List[NodeWithScore]:
        contexts = self._queries[query.query_str]["context"]
        node_with_scores = []
        for ctx in contexts:
            text_list = ctx[1]
            text = "\n".join(text_list)
            node = TextNode(text=text, metadata={"title": ctx[0]})
            node_with_scores.append(NodeWithScore(node=node, score=1.0))

        return node_with_scores

    def __str__(self) -> str:
        return "HotpotQARetriever"


COLBERT_PUBLIC_WIKIPEDIA_ENDPOINT = "http://index.contextual.ai:8893/api/search\
?query={query}&top_k={top_k}"


class ColbertV2WikipediaRetriever(BaseRetriever):
    """
    This is a mocked retriever using a public retriever endpoint for the
    Wikipedia corpus that has been indexed by ColbertV2/PLAID.

    This endpoint is provided in the DSP repo and notebooks:
    https://github.com/stanfordnlp/dsp/
    """

    def _retrieve(self, query: QueryBundle) -> List[NodeWithScore]:
        res = requests.get(
            COLBERT_PUBLIC_WIKIPEDIA_ENDPOINT.format(query=query.query_str, top_k=10)
        )
        obj = json.loads(res.text)

        node_with_scores = []
        for item in obj["topk"]:
            node = TextNode(text=item["text"])
            node_with_scores.append(NodeWithScore(node=node, score=item["score"]))

        return node_with_scores


"""
Utils from https://github.com/hotpotqa/hotpot/blob/master/hotpot_evaluate_v1.py
"""


def normalize_answer(s: str) -> str:
    def remove_articles(text: str) -> str:
        return re.sub(r"\b(a|an|the)\b", " ", text)

    def white_space_fix(text: str) -> str:
        return " ".join(text.split())

    def remove_punc(text: str) -> str:
        exclude = set(string.punctuation)
        return "".join(ch for ch in text if ch not in exclude)

    def lower(text: str) -> str:
        return text.lower()

    return white_space_fix(remove_articles(remove_punc(lower(s))))


def f1_score(prediction: str, ground_truth: str) -> Tuple[float, float, float]:
    normalized_prediction = normalize_answer(prediction)
    normalized_ground_truth = normalize_answer(ground_truth)

    ZERO_METRIC = (0, 0, 0)

    if (
        normalized_prediction in ["yes", "no", "noanswer"]
        and normalized_prediction != normalized_ground_truth
    ):
        return ZERO_METRIC
    if (
        normalized_ground_truth in ["yes", "no", "noanswer"]
        and normalized_prediction != normalized_ground_truth
    ):
        return ZERO_METRIC

    prediction_tokens = normalized_prediction.split()
    ground_truth_tokens = normalized_ground_truth.split()
    common = Counter(prediction_tokens) & Counter(ground_truth_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return ZERO_METRIC
    precision = 1.0 * num_same / len(prediction_tokens)
    recall = 1.0 * num_same / len(ground_truth_tokens)
    f1 = (2 * precision * recall) / (precision + recall)
    return f1, precision, recall


def exact_match_score(prediction: str, ground_truth: str) -> bool:
    return normalize_answer(prediction) == normalize_answer(ground_truth)


def replace_retriever(
    query_engine: BaseQueryEngine, retriever: BaseRetriever
) -> BaseQueryEngine:
    if isinstance(query_engine, RetrieverQueryEngine):
        return query_engine.with_retriever(retriever=retriever)
    elif isinstance(query_engine, MultiStepQueryEngine):
        engine = copy(query_engine)  # shallow copy
        engine._query_engine = replace_retriever(engine._query_engine, retriever)
        return engine
    else:
        raise ValueError("{type(query_engine)} is not supported")
