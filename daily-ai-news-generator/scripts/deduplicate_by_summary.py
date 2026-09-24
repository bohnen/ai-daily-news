#!/usr/bin/env python3
"""
Daily AI News summary-level deduplication script.

Uses embeddings from the OpenAI-compatible LLM endpoint (LLM_BASE_URL/embeddings) to
annotate near-duplicate articles after summary generation. Embeds the original
title/text rather than the Japanese summary. No local model is needed.
When multiple similar articles are found, the article with the longest summary is marked
as the representative and the rest are marked as duplicate candidates.
"""

from __future__ import annotations

import json
import os
import hashlib
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests
from dotenv import load_dotenv

import typesafe_triage

REPO_ROOT = Path(__file__).resolve().parents[2]
INPUT_JSON = REPO_ROOT / "daily-ai-news-generator" / "output" / "daily_articles.json"
ENV_PATH = REPO_ROOT / "daily-ai-news-generator" / "llm.env"
SECRETS_PATH = REPO_ROOT / "daily-ai-news-generator" / "secrets.env"
DEFAULT_MODEL_NAME = "qwen/qwen3-embedding-8b"
# 閾値は qwen3-embedding-8b で原文を埋め込んだときの分布に合わせている
# （実重複 0.73-0.91、非重複の99%点 0.70-0.80）。モデルを変えたら測り直す。
DEFAULT_SIMILARITY_THRESHOLD = 0.78
# この範囲の類似度のペアだけ TypeSafe で「同一事象か」を確認する
DEDUP_LOW = 0.65
DEDUP_HIGH = 0.90
MAX_BOUNDARY_PAIRS = 200
EMBED_BATCH_SIZE = 16
EMBED_HTTP_RETRIES = 3

load_dotenv(ENV_PATH)
load_dotenv(SECRETS_PATH)


class UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        root_a = self.find(a)
        root_b = self.find(b)
        if root_a != root_b:
            self.parent[root_b] = root_a


def get_model_name() -> str:
    return os.environ.get("SUMMARY_DEDUP_MODEL", DEFAULT_MODEL_NAME)


def embed_texts(model_name: str, texts: list[str]) -> np.ndarray:
    """LLM_BASE_URL の /embeddings で正規化済み埋め込みを返す。"""
    base_url = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("LLM_API_KEY", "")
    if not base_url:
        raise RuntimeError(f"LLM_BASE_URL が設定されていません。{ENV_PATH} を確認してください。")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[start:start + EMBED_BATCH_SIZE]
        for attempt in range(1, EMBED_HTTP_RETRIES + 1):
            try:
                resp = requests.post(
                    f"{base_url}/embeddings",
                    headers=headers,
                    json={"model": model_name, "input": batch},
                    timeout=120,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                data = sorted(resp.json()["data"], key=lambda item: item["index"])
                vectors.extend(item["embedding"] for item in data)
                break
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                if attempt == EMBED_HTTP_RETRIES:
                    raise RuntimeError(f"embedding request failed: {e}") from e
                print(f"  [EMBED RETRY {attempt}] {e}")
                time.sleep(2 ** attempt)

    matrix = np.asarray(vectors, dtype=np.float32)
    return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


def embedding_text(article: dict) -> str:
    """埋め込む対象は日本語要約ではなく原文（TypeSafe と同じ入力）。"""
    return typesafe_triage.build_state(article)


def get_similarity_threshold() -> float:
    raw_value = os.environ.get(
        "SUMMARY_DEDUP_THRESHOLD",
        str(DEFAULT_SIMILARITY_THRESHOLD),
    )
    try:
        threshold = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"Invalid SUMMARY_DEDUP_THRESHOLD: {raw_value!r}") from exc

    if not 0.0 <= threshold <= 1.0:
        raise ValueError("SUMMARY_DEDUP_THRESHOLD must be between 0 and 1")

    return threshold


def load_data() -> dict:
    with INPUT_JSON.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict) -> None:
    with INPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def article_id(article: dict) -> str:
    raw = "||".join(
        [
            str(article.get("category", article.get("_category", ""))),
            str(article.get("source", "")),
            str(article.get("title", "")),
            str(article.get("url", "")),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def flatten_articles(data: dict) -> list[dict]:
    flat_articles: list[dict] = []
    for category, articles in data.get("categories", {}).items():
        for article in articles:
            article["_category"] = category
            flat_articles.append(article)
    return flat_articles


def summary_sort_key(article: dict) -> tuple:
    return (
        len(article.get("summary", "")),
        len(article.get("text", "")),
        article.get("date_raw", ""),
        article.get("title", ""),
    )


def judge_boundary_pairs(similarities: np.ndarray, articles: list[dict]) -> dict[tuple[int, int], bool]:
    """境界帯 (DEDUP_LOW <= sim < DEDUP_HIGH) のペアを TypeSafe で確認する。

    戻り値は {(i, j): 同一事象か}。キー未設定・失敗・上限超過のペアは含めない
    （呼び出し側は従来の閾値判定にフォールバックする）。
    state には日本語要約ではなく原文の title/text を使う。
    """
    if not typesafe_triage.is_enabled():
        return {}

    size = similarities.shape[0]
    pairs = [
        (i, j)
        for i in range(size)
        for j in range(i + 1, size)
        if DEDUP_LOW <= similarities[i, j] < DEDUP_HIGH
    ]
    pairs.sort(key=lambda pair: similarities[pair], reverse=True)
    if len(pairs) > MAX_BOUNDARY_PAIRS:
        print(f"  [WARN] 境界ペア {len(pairs)}件のうち上位 {MAX_BOUNDARY_PAIRS}件のみ TypeSafe で確認します。")
        pairs = pairs[:MAX_BOUNDARY_PAIRS]

    with ThreadPoolExecutor(max_workers=8) as executor:
        probs = list(executor.map(lambda pair: typesafe_triage.same_event(articles[pair[0]], articles[pair[1]]), pairs))

    verdicts = {}
    for (i, j), prob in zip(pairs, probs):
        if prob is None:
            continue
        verdicts[(i, j)] = prob >= typesafe_triage.SAME_EVENT_THRESHOLD
        print(
            f"  [BOUNDARY] sim={similarities[i, j]:.2f} noul={prob:.2f} "
            f"{'同一' if verdicts[(i, j)] else '別件'}: "
            f"{articles[i].get('title', '')[:40]} / {articles[j].get('title', '')[:40]}"
        )
    return verdicts


def is_duplicate_pair(similarity: float, threshold: float, verdict: bool | None) -> bool:
    """高類似度は即重複、境界帯は TypeSafe の判定を優先、判定なしは従来の閾値。"""
    if similarity >= DEDUP_HIGH or verdict is None:
        return similarity >= threshold
    return verdict


def collect_clusters(
    similarities: np.ndarray,
    threshold: float,
    verdicts: dict[tuple[int, int], bool] | None = None,
) -> dict[int, list[int]]:
    verdicts = verdicts or {}
    size = similarities.shape[0]
    union_find = UnionFind(size)

    for i in range(size):
        for j in range(i + 1, size):
            if is_duplicate_pair(similarities[i, j], threshold, verdicts.get((i, j))):
                union_find.union(i, j)

    clusters: dict[int, list[int]] = defaultdict(list)
    for index in range(size):
        clusters[union_find.find(index)].append(index)
    return clusters


def reset_duplicate_metadata(articles: list[dict]) -> None:
    for article in articles:
        article["article_id"] = article_id(article)
        article["is_duplicate_candidate"] = False
        article["duplicate_of"] = None
        article["duplicate_score"] = None
        article["duplicate_confirmed_by"] = None
        article["duplicate_count"] = 0
        article["duplicate_group_id"] = article["article_id"]


def clear_internal_fields(data: dict) -> None:
    for articles in data.get("categories", {}).values():
        for article in articles:
            article.pop("_category", None)


def main() -> dict:
    data = load_data()
    model_name = get_model_name()
    threshold = get_similarity_threshold()
    flat_articles = flatten_articles(data)
    reset_duplicate_metadata(flat_articles)

    print("=== Step: サマリー類似度による重複排除 ===")
    print(f"入力記事数: {len(flat_articles)}件")
    print(f"埋め込みモデル: {model_name}")
    print(f"類似度閾値: {threshold:.2f}")

    if len(flat_articles) <= 1:
        print("記事数が1件以下のためスキップします。")
        data["total"] = len(flat_articles)
        data.setdefault("stats", {})["after_summary_dedup"] = len(flat_articles)
        data["stats"]["duplicate_candidates"] = 0
        data["stats"]["visible_after_summary_dedup"] = len(flat_articles)
        clear_internal_fields(data)
        save_data(data)
        return data

    embeddings = embed_texts(model_name, [embedding_text(article) for article in flat_articles])

    similarities = embeddings @ embeddings.T
    verdicts = judge_boundary_pairs(similarities, flat_articles)
    clusters = collect_clusters(similarities, threshold, verdicts)

    duplicate_count = 0

    for cluster_indices in clusters.values():
        if len(cluster_indices) == 1:
            continue

        cluster_articles = [flat_articles[index] for index in cluster_indices]
        representative = max(cluster_articles, key=summary_sort_key)
        representative_id = representative["article_id"]
        representative["duplicate_group_id"] = representative_id
        kept_duplicates = 0

        print(
            f"  [CLUSTER] {len(cluster_articles)}件 -> 1件採用: "
            f"{representative.get('title', '')[:80]}"
        )

        rep_index = flat_articles.index(representative)
        for article in cluster_articles:
            if article is representative:
                continue
            article_index = flat_articles.index(article)
            score_to_rep = round(float(similarities[rep_index, article_index]), 4)
            verdict = verdicts.get((min(rep_index, article_index), max(rep_index, article_index)))
            if not is_duplicate_pair(score_to_rep, threshold, verdict):
                continue
            duplicate_count += 1
            kept_duplicates += 1
            article["is_duplicate_candidate"] = True
            article["duplicate_of"] = representative_id
            article["duplicate_group_id"] = representative_id
            article["duplicate_score"] = score_to_rep
            article["duplicate_confirmed_by"] = "embedding" if verdict is None else "typesafe"

        representative["duplicate_count"] = kept_duplicates

    total_articles = len(flat_articles)
    visible_articles = total_articles - duplicate_count
    data["total"] = total_articles
    data.setdefault("stats", {})["after_summary_dedup"] = total_articles
    data["stats"]["duplicate_candidates"] = duplicate_count
    data["stats"]["visible_after_summary_dedup"] = visible_articles
    clear_internal_fields(data)

    save_data(data)

    print(
        f"サマリー類似度による重複注釈完了: "
        f"{total_articles}件中 {duplicate_count}件を重複候補としてマーク"
    )
    print(f"保存完了: {INPUT_JSON}")
    return data


if __name__ == "__main__":
    main()
