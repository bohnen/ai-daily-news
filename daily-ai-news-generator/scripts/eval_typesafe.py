#!/usr/bin/env python3
"""
TypeSafe 判定のオフライン評価。

output/daily_articles.json（LLM が AI 関連と判定 = 正例）と
output/rejected_articles.json（LLM が除外 = 負例）に triage() をかけ、
LLM 判定との一致率・閾値スイープ・カテゴリ一致・タグ分布・コストを出力する。
本番の出力ファイルは変更しない。結果の詳細は output/typesafe_eval.json に保存する。
"""

import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import typesafe_triage as ts

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = REPO_ROOT / "daily-ai-news-generator" / "output"
ARTICLES_JSON = OUTPUT_DIR / "daily_articles.json"
REJECTED_JSON = OUTPUT_DIR / "rejected_articles.json"
EVAL_JSON = OUTPUT_DIR / "typesafe_eval.json"
PRICE_PER_MILLION_INPUT_TOKENS = 0.042


def load_articles():
    accepted = []
    with ARTICLES_JSON.open(encoding="utf-8") as f:
        for articles in json.load(f)["categories"].values():
            accepted.extend(articles)
    rejected = []
    if REJECTED_JSON.is_file():
        with REJECTED_JSON.open(encoding="utf-8") as f:
            rejected = json.load(f)["articles"]
    return accepted, rejected


def is_japanese_source(article):
    text = f"{article.get('title', '')} {article.get('text', '')}"
    return len(re.findall(r"[぀-ヿ㐀-鿿]", text)) >= 20


def main():
    if not ts.is_enabled():
        raise SystemExit("TYPESAFE_API_KEY が設定されていません。")

    accepted, rejected = load_articles()
    rows = [(art, True) for art in accepted] + [(art, False) for art in rejected]
    print(f"評価対象: 採用 {len(accepted)}件 / 除外 {len(rejected)}件")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda row: ts.triage(row[0]), rows))

    records = []
    for (art, llm_ai), result in zip(rows, results):
        if result is None:
            continue
        records.append({
            "title": art["title"],
            "source": art["source"],
            "feed_group": art.get("feed_group"),
            "llm_ai": llm_ai,
            "llm_reason": art.get("reason", ""),
            "japanese": is_japanese_source(art),
            **result,
        })
    print(f"TypeSafe 応答: {len(records)}/{len(rows)}件")
    if not records:
        raise SystemExit("評価できる記事がありません。")

    print("\n=== AI関連判定: 閾値スイープ（reject <= r, accept >= a）===")
    print("  reject accept | 自動採用(うち誤) 自動除外(うち誤) 中間帯  LLM呼び出し削減")
    for reject, accept in ((0.2, 0.8), (0.3, 0.7), (0.4, 0.6), (0.5, 0.5)):
        auto_accept = [r for r in records if r["ai_prob"] >= accept]
        auto_reject = [r for r in records if r["ai_prob"] <= reject and r["ai_prob"] < accept]
        wrong_accept = sum(1 for r in auto_accept if not r["llm_ai"])
        wrong_reject = sum(1 for r in auto_reject if r["llm_ai"])
        uncertain = len(records) - len(auto_accept) - len(auto_reject)
        print(
            f"  {reject:>6} {accept:>6} | {len(auto_accept):>4} ({wrong_accept:>2})"
            f"        {len(auto_reject):>4} ({wrong_reject:>2})        {uncertain:>4}"
            f"    {len(auto_reject) / len(records):.0%}"
        )

    print(f"\n=== 現行閾値 ({ts.AI_REJECT}/{ts.AI_ACCEPT}) で LLM と食い違う記事 ===")
    for r in sorted(records, key=lambda r: r["ai_prob"]):
        route = ts.route_ai(r["ai_prob"])
        if (route == "accept" and not r["llm_ai"]) or (route == "reject" and r["llm_ai"]):
            print(f"  noul={r['ai_prob']:.2f} LLM={'AI' if r['llm_ai'] else '非AI'} [{r['source']}] {r['title'][:60]}")
            if r["llm_reason"]:
                print(f"      LLM理由: {r['llm_reason'][:80]}")

    print("\n=== 言語別 ===")
    for label, flag in (("英語ソース", False), ("日本語ソース", True)):
        subset = [r for r in records if r["japanese"] == flag]
        if subset:
            agree = sum(1 for r in subset if (r["ai_prob"] >= 0.5) == r["llm_ai"])
            print(f"  {label}: {len(subset)}件 / 0.5閾値での一致 {agree / len(subset):.0%}")

    ai_records = [r for r in records if r["llm_ai"]]
    if ai_records:
        print("\n=== カテゴリ（採用記事のみ）===")
        resolved = Counter(ts.resolve_category(r) for r in ai_records)
        for category in ts.CATEGORY_ORDER:
            print(f"  {category}: {resolved.get(category, 0)}件")
        others = [r for r in ai_records if ts.resolve_category(r) == ts.OTHER_CATEGORY]
        print(f"  「{ts.OTHER_CATEGORY}」の割合: {len(others) / len(ai_records):.0%}（増えてきたらカテゴリ体系の見直しを検討）")
        for r in sorted(others, key=lambda r: r["category_confidence"]):
            print(f"    confidence={r['category_confidence']:.2f} 第一候補={r['category']} [{r['source']}] {r['title'][:50]}")

        print("\n=== タグ ===")
        tag_counts = Counter(tag for r in ai_records for tag in r["tags"])
        per_article = [len(r["tags"]) for r in ai_records]
        print(f"  1記事あたり 平均 {sum(per_article) / len(per_article):.1f}個 / 最大 {max(per_article)}個 / タグなし {per_article.count(0)}件")
        print("  " + ", ".join(f"{ts.TAGS[tag][0]}:{count}" for tag, count in tag_counts.most_common()))
        broad = [ts.TAGS[tag][0] for tag, count in tag_counts.items() if count / len(ai_records) > 0.2]
        if broad:
            print(f"  [WARN] 2割超の記事に付いたタグ（広すぎる可能性）: {', '.join(broad)}")
        unused = [name for tag, (name, _) in ts.TAGS.items() if tag not in tag_counts]
        print(f"  未使用: {', '.join(unused) or 'なし'}")

        print("\n=== 重要度 上位10 ===")
        for r in sorted(ai_records, key=lambda r: r["importance"], reverse=True)[:10]:
            s = r["scores"]
            print(f"  {r['importance']:.2f} (P{s['practical_value']:.1f} N{s['novelty']:.1f} T{s['technical_depth']:.1f}) {r['title'][:60]}")

    tokens = ts.usage["input_tokens"]
    print("\n=== コスト ===")
    print(f"  {ts.usage['requests']}リクエスト / 入力 {tokens:,}トークン / 失敗 {ts.usage['failures']}件")
    print(f"  この実行: ${tokens / 1_000_000 * PRICE_PER_MILLION_INPUT_TOKENS:.4f}")

    with EVAL_JSON.open("w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"\n詳細を保存: {EVAL_JSON}")


if __name__ == "__main__":
    main()
