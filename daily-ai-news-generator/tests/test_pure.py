"""ネットワーク不要の純粋関数テスト。

実行: uv run --with pytest pytest daily-ai-news-generator/tests
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("LLM_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest

import deduplicate_by_summary as dedup
import fetch_daily
import typesafe_triage as ts

REQUIRED = ("summary", "is_ai_related")


def test_extract_json_ignores_think_and_leading_braces():
    raw = '<think>{"x": 1}</think> note {bad} {"summary": "a", "is_ai_related": true}'
    assert fetch_daily.extract_json_object(raw, REQUIRED)["summary"] == "a"


def test_extract_json_handles_fence_and_nested_object():
    raw = '```json\n{"summary": "a", "is_ai_related": false, "meta": {"k": 1}}\n```'
    assert fetch_daily.extract_json_object(raw, REQUIRED)["meta"] == {"k": 1}


def test_extract_json_rejects_missing_required_key():
    with pytest.raises(ValueError):
        fetch_daily.extract_json_object('{"summary": "a"}', REQUIRED)


def test_route_ai_thresholds():
    assert ts.route_ai(ts.AI_ACCEPT) == "accept"
    assert ts.route_ai(ts.AI_REJECT) == "reject"
    assert ts.route_ai(0.5) == "uncertain"
    assert ts.route_ai(None) == "uncertain"


def test_importance_is_normalized():
    assert ts.compute_importance({"practical_value": 4, "novelty": 4, "technical_depth": 4}) == 1.0
    assert ts.compute_importance({"practical_value": 0, "novelty": 0, "technical_depth": 0}) == 0.0


def test_low_confidence_category_goes_to_other():
    base = {"ai_prob": 0.9, "importance": 0.5, "scores": {}, "tags": ["llm"], "category": "政策・社会"}
    confident = {}
    fetch_daily.apply_triage(confident, {**base, "category_confidence": ts.CATEGORY_MIN_CONFIDENCE})
    assert confident["category"] == "政策・社会" and "category_candidate" not in confident

    unsure = {}
    fetch_daily.apply_triage(unsure, {**base, "category_confidence": ts.CATEGORY_MIN_CONFIDENCE - 0.01})
    assert unsure["category"] == ts.OTHER_CATEGORY and unsure["category_candidate"] == "政策・社会"


def test_unknown_category_goes_to_other():
    assert ts.resolve_category({"category": "存在しない", "category_confidence": 1.0}) == ts.OTHER_CATEGORY


def test_apply_triage_without_result():
    article = {}
    fetch_daily.apply_triage(article, None)
    assert article["category"] == ts.OTHER_CATEGORY
    assert article["importance"] is None and article["tags"] == []


def test_duplicate_pair_rules():
    threshold = 0.65
    assert dedup.is_duplicate_pair(0.9, threshold, False)       # 高類似度は TypeSafe に関係なく重複
    assert not dedup.is_duplicate_pair(0.7, threshold, False)   # 境界帯は TypeSafe の否定を優先
    assert dedup.is_duplicate_pair(0.6, threshold, True)        # 閾値未満でも TypeSafe が同一と判定
    assert dedup.is_duplicate_pair(0.7, threshold, None)        # 判定なしは従来の閾値
    assert not dedup.is_duplicate_pair(0.6, threshold, None)
