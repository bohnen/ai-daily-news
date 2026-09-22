#!/usr/bin/env python3
"""
Daily AI News - フィード取得・重複排除・サマリー生成スクリプト
ai-news-feedsスキルの全フィードから過去24時間の記事を収集する

フィード一覧はai-news-feedsスキル（/home/ubuntu/skills/ai-news-feeds/SKILL.md）をベースにしつつ、
ローカル追加の公式RSSも含む（全44件）。

処理フロー:
  1. 全フィードから記事取得
  2. タイトルベースの重複排除（URL一致 + 文字列類似度）
  3. LLM（OpenAI互換Chat Completions）でサマリー生成
  4. LLM（OpenAI互換Chat Completions）でAI関連フィルタリング
  5. JSON出力
"""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv

import typesafe_triage

# ===== 設定 =====
DAYS_BACK = 1
OLSHANSK_BASE = "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/"
HEADERS = {"User-Agent": "feedparser/6.0"}

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = REPO_ROOT / "daily-ai-news-generator" / "output"
OUTPUT_JSON = OUTPUT_DIR / "daily_articles.json"
ENV_PATH = REPO_ROOT / "daily-ai-news-generator" / "llm.env"
SECRETS_PATH = REPO_ROOT / "daily-ai-news-generator" / "secrets.env"
REJECTED_JSON = OUTPUT_DIR / "rejected_articles.json"
LLM_HTTP_RETRIES = 3
DEFAULT_SUMMARY_CONCURRENCY = 3
DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS = 500

# ===== フィード定義（ai-news-feedsスキルと完全一致） =====
# キーはフィードのグループ名（記事の feed_group）。記事のカテゴリはフィードではなく
# 内容で決める（typesafe_triage.CATEGORIES + 「その他」）。

FEED_CATEGORIES = {
    # --- Olshansk/rss-feeds: Anthropic関連 ---
    "Anthropic": [
        ("Claude",                   OLSHANSK_BASE + "feed_claude.xml"),
        ("Claude Code Changelog",    "https://code.claude.com/docs/en/changelog/rss.xml"),
        ("Anthropic Engineering",    OLSHANSK_BASE + "feed_anthropic_engineering.xml"),
        ("Anthropic News",           OLSHANSK_BASE + "feed_anthropic_news.xml"),
        ("Anthropic RED",            OLSHANSK_BASE + "feed_anthropic_red.xml"),
        ("Anthropic Research",       OLSHANSK_BASE + "feed_anthropic_research.xml"),
    ],
    # --- Olshansk/rss-feeds: AI開発ツール ---
    "AI開発ツール": [
        ("Cursor",                   OLSHANSK_BASE + "feed_cursor.xml"),
        ("Ollama",                   OLSHANSK_BASE + "feed_ollama.xml"),
        ("Windsurf Blog",            OLSHANSK_BASE + "feed_windsurf_blog.xml"),
        ("Windsurf Changelog",       OLSHANSK_BASE + "feed_windsurf_changelog.xml"),
        ("Windsurf Next Changelog",  OLSHANSK_BASE + "feed_windsurf_next_changelog.xml"),
    ],
    # --- Olshansk/rss-feeds: その他 + AIベンダー公式 ---
    "AIベンダー": [
        ("Google AI (Olshansk)",     OLSHANSK_BASE + "feed_google_ai.xml"),
        ("xAI News",                 OLSHANSK_BASE + "feed_xainews.xml"),
        ("Thinking Machines",        OLSHANSK_BASE + "feed_thinkingmachines.xml"),
        ("BlogSurge AI",             OLSHANSK_BASE + "feed_blogsurgeai.xml"),
        ("Dagster",                  OLSHANSK_BASE + "feed_dagster.xml"),
        ("OpenAI Research",          "https://openai.com/blog/rss.xml"),
        ("OpenAI News",              "https://openai.com/news/rss.xml"),
        ("Google DeepMind",          "https://deepmind.google/blog/rss.xml"),
        ("Google AI Blog",           "https://blog.google/innovation-and-ai/technology/ai/rss/"),
        ("Microsoft AI",             "https://news.microsoft.com/source/topics/ai/feed/"),
        ("Microsoft AI Models",      "https://microsoft.ai/news-categories/models/feed/"),
        ("AWS ML Blog",              "https://aws.amazon.com/blogs/machine-learning/feed/"),
        ("Hugging Face Blog",        "https://huggingface.co/blog/feed.xml"),
        ("NVIDIA Deep Learning",     "https://blogs.nvidia.com/blog/category/deep-learning/feed/"),
        ("NVIDIA Generative AI",     "https://developer.nvidia.com/blog/category/generative-ai/feed/rss2/"),
    ],
    # --- AIニュースサイト ---
    "AIニュース・メディア": [
        ("MIT Technology Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed/"),
        ("VentureBeat",              "https://venturebeat.com/feed"),
        ("TechCrunch AI",            "https://techcrunch.com/category/artificial-intelligence/feed"),
        ("WIRED AI",                 "https://www.wired.com/feed/tag/ai/latest/rss"),
        ("The Verge",                "https://www.theverge.com/rss/index.xml"),
        ("Ars Technica AI",          "https://arstechnica.com/ai/feed/"),
        ("AI News",                  "https://www.artificialintelligence-news.com/feed/"),
        ("The Batch (DeepLearning.AI)", OLSHANSK_BASE + "feed_the_batch.xml"),
    ],
    # --- 研究者ブログ・ニュースレター ---
    "研究者・ニュースレター": [
        ("Import AI (Jack Clark)",   "https://importai.substack.com/feed"),
        ("The Gradient",             "https://thegradient.pub/rss/"),
        ("Towards Data Science",     "https://towardsdatascience.com/feed"),
        ("Ahead of AI (S. Raschka)", "https://magazine.sebastianraschka.com/feed"),
        ("Simon Willison",           "https://simonwillison.net/atom/everything/"),
        ("Andrej Karpathy",          "https://karpathy.substack.com/feed"),
        ("Last Week in AI",          "https://lastweekin.ai/feed"),
        ("Chander Ramesh",           OLSHANSK_BASE + "feed_chanderramesh.xml"),
        ("Hamel Husain",             "https://hamel.dev/index.xml"),
        ("Paul Graham",              OLSHANSK_BASE + "feed_paulgraham.xml"),
    ],
}

# 既に export 済みの環境変数は上書きしない（秘密値は secrets.env か環境変数で渡す）
load_dotenv(ENV_PATH)
load_dotenv(SECRETS_PATH)
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_DISABLE_REASONING = os.environ.get("LLM_DISABLE_REASONING", "1") not in ("", "0", "false")

if not LLM_BASE_URL or not LLM_MODEL:
    raise RuntimeError(
        f"LLM_BASE_URL / LLM_MODEL が設定されていません。{ENV_PATH} を確認してください。"
    )
if not LLM_API_KEY and not re.match(r"https?://(127\.0\.0\.1|localhost)[:/]", LLM_BASE_URL):
    raise RuntimeError(
        f"LLM_API_KEY が設定されていません。環境変数か {SECRETS_PATH} で指定してください。"
    )

# ─── ユーティリティ ────────────────────────────────────────────────────────

def get_cutoff():
    return datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)

def parse_time(entry):
    for field in ['published_parsed', 'updated_parsed']:
        t = getattr(entry, field, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None

def fetch_feed(name, url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return feedparser.parse(resp.content)
    except Exception as e:
        print(f"  [SKIP] {name}: {e}")
        return None

def get_text(entry):
    content = ""
    if hasattr(entry, 'content') and entry.content:
        content = entry.content[0].get('value', '')
    elif hasattr(entry, 'summary'):
        content = entry.summary
    content = re.sub(r'<[^>]+>', ' ', content)
    content = re.sub(r'\s+', ' ', content).strip()
    return content[:2000]

def format_jst(dt):
    if dt:
        jst = dt + timedelta(hours=9)
        return jst.strftime("%Y-%m-%d %H:%M JST")
    return "日時不明"

def log_now():
    return (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%H:%M:%S")

def strip_think(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    return re.sub(r"^.*?</think>", "", text, flags=re.S).strip()

def extract_json_object(text, required=()):
    text = strip_think(text)
    if not text:
        raise ValueError("empty response")
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    end = text.rfind("}")
    if end == -1:
        raise ValueError("json object not found")
    # 前置きテキスト中の "{" に惑わされないよう、後ろの "{" から順に試す
    start = text.rfind("{", 0, end)
    while start != -1:
        try:
            payload = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            start = text.rfind("{", 0, start)
            continue
        if isinstance(payload, dict) and all(key in payload for key in required):
            return payload
        start = text.rfind("{", 0, start)
    raise ValueError("json object not found")

def get_summary_concurrency():
    raw_value = os.environ.get("SUMMARY_CONCURRENCY", str(DEFAULT_SUMMARY_CONCURRENCY))
    try:
        concurrency = int(raw_value)
    except (TypeError, ValueError):
        print(
            f"[WARN] SUMMARY_CONCURRENCY={raw_value!r} は不正です。"
            f"デフォルト値 {DEFAULT_SUMMARY_CONCURRENCY} を使用します。"
        )
        return DEFAULT_SUMMARY_CONCURRENCY

    if concurrency < 1:
        print(
            f"[WARN] SUMMARY_CONCURRENCY={concurrency} は 1 以上を指定してください。"
            f"デフォルト値 {DEFAULT_SUMMARY_CONCURRENCY} を使用します。"
        )
        return DEFAULT_SUMMARY_CONCURRENCY

    return concurrency

def get_summary_max_output_tokens():
    raw_value = os.environ.get(
        "SUMMARY_MAX_OUTPUT_TOKENS", str(DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS)
    )
    try:
        max_output_tokens = int(raw_value)
    except (TypeError, ValueError):
        print(
            f"[WARN] SUMMARY_MAX_OUTPUT_TOKENS={raw_value!r} は不正です。"
            f"デフォルト値 {DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS} を使用します。"
        )
        return DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS

    if max_output_tokens < 1:
        print(
            f"[WARN] SUMMARY_MAX_OUTPUT_TOKENS={max_output_tokens} は 1 以上を指定してください。"
            f"デフォルト値 {DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS} を使用します。"
        )
        return DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS

    return max_output_tokens

# ─── Step 2: タイトルベースの重複排除（高速・LLM不要） ──────────────────────

def normalize_title(title):
    normalized = title.lower().strip()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"[“”\"'‘’]", "", normalized)
    normalized = re.sub(r"\s*[-:|]\s.*$", "", normalized)
    normalized = re.sub(r"[^a-z0-9\u3040-\u30ff\u3400-\u9fff\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized

def is_title_duplicate(title1, title2, threshold=0.75):
    normalized1 = normalize_title(title1)
    normalized2 = normalize_title(title2)
    if not normalized1 or not normalized2:
        return False
    if normalized1 == normalized2:
        return True
    ratio = SequenceMatcher(None, normalized1, normalized2).ratio()
    return ratio >= threshold

def deduplicate_by_title(articles):
    """URL一致 + タイトル文字列類似度による重複排除"""
    seen_urls = set()
    seen_titles = []
    result = []
    for art in articles:
        url = art.get("url", "")
        title = art.get("title", "")
        if url and url in seen_urls:
            continue
        if any(is_title_duplicate(title, t) for t in seen_titles):
            continue
        if url:
            seen_urls.add(url)
        seen_titles.append(title)
        result.append(art)
    return result

# ─── Step 3: サマリー生成 ────────────────────────────────────────────────────

JSON_SYSTEM_PROMPT = "You are a JSON generator. No talk. No code blocks. Output exactly one JSON object. Always use Japanese for text values."
SUMMARY_FILTER_JSON_SCHEMA = {
    "name": "summary_filter_result",
    "schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "is_ai_related": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["summary", "is_ai_related", "reason"],
        "additionalProperties": False,
    },
    "strict": True,
}
SUMMARY_FILTER_REQUIRED = ("summary", "is_ai_related")

def generate_text(
    prompt,
    max_output_tokens=300,
    temperature=0.3,
    system_prompt=None,
    json_mode=False,
    json_schema=None,
):
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    request_kwargs = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_output_tokens,
    }
    if json_schema:
        request_kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": json_schema,
        }
    elif json_mode:
        request_kwargs["response_format"] = {"type": "json_object"}
    if LLM_DISABLE_REASONING:
        request_kwargs["reasoning"] = {"enabled": False}

    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    for attempt in range(1, LLM_HTTP_RETRIES + 1):
        retry_after = None
        try:
            resp = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers=headers,
                json=request_kwargs,
                timeout=120,
            )
            if resp.status_code != 429 and resp.status_code < 500:
                resp.raise_for_status()
                data = resp.json()
                content = (data["choices"][0]["message"].get("content") or "").strip()
                if not content:
                    raise ValueError("empty content")
                return content
            error = f"HTTP {resp.status_code}"
            retry_after = resp.headers.get("Retry-After")
        except (requests.ConnectionError, requests.Timeout) as e:
            error = str(e)
        if attempt == LLM_HTTP_RETRIES:
            raise RuntimeError(f"LLM request failed: {error}")
        delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
        print(f"  [{log_now()}] [LLM RETRY {attempt}] {error} / {delay:.0f}s待機")
        time.sleep(min(delay, 60))

def normalize_summary_text(text):
    summary = text.strip()
    summary = re.sub(r"^\s*(日本語サマリー|要約)\s*[:：]\s*", "", summary)
    summary = re.sub(r"^\s*サマリー\s*[:：]\s*", "", summary)
    summary = re.sub(r"^「", "", summary)
    summary = re.sub(r"」$", "", summary)
    return summary.strip()

def count_summary_sentences(summary):
    return len([s for s in re.split(r"[。.!?]+", summary) if s.strip()])

def is_summary_primarily_japanese(summary):
    if not summary:
        return False
    japanese_chars = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", summary))
    latin_letters = len(re.findall(r"[A-Za-z]", summary))
    return japanese_chars >= max(20, latin_letters)

def is_summary_in_range(summary, min_len=140, max_len=240, min_sentences=2, max_sentences=4):
    sentence_count = count_summary_sentences(summary)
    return min_len <= len(summary) <= max_len and min_sentences <= sentence_count <= max_sentences

def finalize_summary_text(summary, min_len=140, max_len=240, max_sentences=4):
    summary = normalize_summary_text(summary)
    sentences = [s.strip() for s in re.split(r"(?<=[。.!?])\s*", summary) if s.strip()]

    if len(sentences) > max_sentences:
        summary = "".join(sentences[:max_sentences]).strip()
        sentences = [s.strip() for s in re.split(r"(?<=[。.!?])\s*", summary) if s.strip()]

    if len(summary) <= max_len:
        return summary

    trimmed = ""
    for sentence in sentences:
        candidate = f"{trimmed}{sentence}".strip()
        if len(candidate) > max_len:
            break
        trimmed = candidate

    if trimmed and len(trimmed) >= min_len:
        return trimmed

    if trimmed and sentences:
        remaining = max_len - len(trimmed)
        if remaining > 10:
            next_sentence = summary[len(trimmed):].strip()
            addition = next_sentence[:remaining].rstrip(" 、,")
            candidate = f"{trimmed}{addition}".strip()
            if candidate and candidate[-1] not in "。.!?":
                candidate += "。"
            if len(candidate) >= min_len:
                return candidate

    if len(summary) > max_len:
        clipped = summary[:max_len].rstrip(" 、,")
        if clipped and clipped[-1] not in "。.!?":
            clipped += "。"
        return clipped

    return summary[:max_len].rstrip()

def summarize_and_filter(title, url, text, source, trust_ai=False):
    # trust_ai: TypeSafe が AI 関連と確定済み。LLM の is_ai_related は使わない。
    max_output_tokens = get_summary_max_output_tokens()
    prompt = f"""Read the article below and return only a single JSON object.
Do not include code blocks, markdown, or any text outside the JSON.

JSON Schema:
{{
  "summary": "A natural 2-4 sentence summary (140-240 characters). Focus on what happened, what is new, and its significance. No headers or quotes.",
  "is_ai_related": boolean,
  "reason": "A brief, one-sentence explanation for why the article is or is not AI-related."
}}

AI Relevance Criteria:
- True: The main topic is AI/ML/LLM, robotics, AI research, AI products, AI companies, or concrete AI use cases.
- False: The main topic is general tech, gadgets, sales, entertainment, politics, sports, or other non-AI news, even if AI is mentioned incidentally.

Article Data:
Source: {source}
Title: {title}
URL: {url}
Content: {text}
"""
    retry_prompt = f"""Fix the response and return only a single JSON object.
Do not include code blocks, markdown, or any text outside the JSON.

Requirements:
- summary must be natural Japanese
- summary must be 140-240 characters and 2-4 sentences
- summary must explain what happened, what is new, and why it matters
- is_ai_related must be a JSON boolean
- reason must be one short Japanese sentence explaining why the article is or is not AI-related

Title: {title}
Content: {text}
"""
    expand_prompt = f"""Rewrite the JSON and return only a single JSON object.
Do not include code blocks, markdown, or any text outside the JSON.

Keep the facts, but make summary longer.
- summary must be natural Japanese
- summary must be 140-240 characters and 2-4 sentences
- summary must explain what happened, what is new, and why it matters

Title: {title}
Content: {text}
Previous JSON:
{{payload}}
"""
    japanese_retry_prompt = f"""Rewrite the JSON and return only a single JSON object.
Do not include code blocks, markdown, or any text outside the JSON.

The summary is not Japanese enough.
- summary must be natural Japanese only
- do not leave English sentences in summary
- summary must be 140-240 characters and 2-4 sentences
- reason must be one short Japanese sentence

Title: {title}
Content: {text}
Previous JSON:
{{payload}}
"""
    translation_retry_prompt = """Rewrite the JSON and return only a single JSON object.
Do not include code blocks, markdown, or any text outside the JSON.

Translate the summary into natural Japanese.
- keep the facts unchanged
- do not leave English sentences in summary
- summary must be 140-240 characters and 2-4 sentences
- reason must be one short Japanese sentence

Previous JSON:
{payload}
"""
    try:
        payload = None
        for attempt, temperature in enumerate((0.3, 0.2, 0.1), start=1):
            current_prompt = prompt if attempt == 1 else retry_prompt
            raw = generate_text(
                current_prompt,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                system_prompt=JSON_SYSTEM_PROMPT,
                json_schema=SUMMARY_FILTER_JSON_SCHEMA,
            )
            try:
                payload = extract_json_object(raw, SUMMARY_FILTER_REQUIRED)
            except ValueError as e:
                print(f"  [{log_now()}] [JSON RETRY {attempt}] {title[:40]}: {e}")
                continue
            summary = finalize_summary_text(str(payload.get("summary", "")))
            if trust_ai and attempt == 1 and not payload.get("is_ai_related"):
                print(f"  [{log_now()}] [判定差分] TypeSafe=AI / LLM=非AI: {title[:50]} / {payload.get('reason', '')}")
            is_ai_related = trust_ai or bool(payload.get("is_ai_related"))
            payload["summary"] = summary
            payload["is_ai_related"] = is_ai_related
            payload["reason"] = str(payload.get("reason", "")).strip()
            # 非AIと判定された記事は要約を使わないので、作り直さず確定する
            if not is_ai_related:
                break
            if is_summary_in_range(summary) and is_summary_primarily_japanese(summary):
                break
        if payload is None:
            raise ValueError("classification payload missing")

        if payload.get("is_ai_related") and len(payload["summary"]) < 140:
            raw = generate_text(
                expand_prompt.format(payload=json.dumps(payload, ensure_ascii=False)),
                max_output_tokens=max_output_tokens,
                temperature=0.2,
                system_prompt=JSON_SYSTEM_PROMPT,
                json_schema=SUMMARY_FILTER_JSON_SCHEMA,
            )
            expanded = extract_json_object(raw)
            payload["summary"] = finalize_summary_text(str(expanded.get("summary", payload["summary"])))
            payload["reason"] = str(expanded.get("reason", payload.get("reason", ""))).strip()
            payload["is_ai_related"] = trust_ai or bool(expanded.get("is_ai_related", payload["is_ai_related"]))

        if payload.get("is_ai_related") and not is_summary_primarily_japanese(payload["summary"]):
            raw = generate_text(
                japanese_retry_prompt.format(payload=json.dumps(payload, ensure_ascii=False)),
                max_output_tokens=max_output_tokens,
                temperature=0.1,
                system_prompt=JSON_SYSTEM_PROMPT,
                json_schema=SUMMARY_FILTER_JSON_SCHEMA,
            )
            rewritten = extract_json_object(raw)
            payload["summary"] = finalize_summary_text(str(rewritten.get("summary", payload["summary"])))
            payload["reason"] = str(rewritten.get("reason", payload.get("reason", ""))).strip()
            payload["is_ai_related"] = trust_ai or bool(rewritten.get("is_ai_related", payload["is_ai_related"]))

        if payload.get("is_ai_related") and not is_summary_primarily_japanese(payload["summary"]):
            raw = generate_text(
                translation_retry_prompt.format(payload=json.dumps(payload, ensure_ascii=False)),
                max_output_tokens=max_output_tokens,
                temperature=0,
                system_prompt=JSON_SYSTEM_PROMPT,
                json_schema=SUMMARY_FILTER_JSON_SCHEMA,
            )
            translated = extract_json_object(raw)
            payload["summary"] = finalize_summary_text(str(translated.get("summary", payload["summary"])))
            payload["reason"] = str(translated.get("reason", payload.get("reason", ""))).strip()
            payload["is_ai_related"] = trust_ai or bool(translated.get("is_ai_related", payload["is_ai_related"]))

        return {
            "summary": finalize_summary_text(str(payload.get("summary", ""))).strip(),
            "is_ai_related": bool(payload.get("is_ai_related")),
            "reason": str(payload.get("reason", "")).strip(),
        }
    except Exception as e:
        print(f"  [{log_now()}] [SUMMARY/FILTER ERROR] {title[:40]}: {e}")
        return {
            "summary": "（サマリー生成に失敗しました）",
            "is_ai_related": True,
            "reason": "AI関連判定に失敗したため記事を維持",
        }

def process_article(index, article):
    title = article["title"]
    print(f"  [{log_now()}] [START {index}] {title[:60]}...")
    triage = typesafe_triage.triage(article)
    route = typesafe_triage.route_ai(triage["ai_prob"] if triage else None)
    if route == "reject":
        # 要約せずに除外する（LLM 呼び出しなし）
        analysis = {
            "summary": "",
            "is_ai_related": False,
            "reason": f"TypeSafe判定で非AI (noul={triage['ai_prob']:.2f})",
        }
    else:
        analysis = summarize_and_filter(
            article["title"],
            article["url"],
            article["text"],
            article["source"],
            trust_ai=(route == "accept"),
        )
    analysis["triage"] = triage
    analysis["route"] = route if triage else "fallback"
    return index, analysis

def apply_triage(article, triage):
    """カテゴリ・重要度・タグを記事に反映する。triage が None なら「その他」と既定値。"""
    article["category"] = typesafe_triage.resolve_category(triage)
    if triage is None:
        article["category_confidence"] = None
        article["ai_prob"] = None
        article["importance"] = None
        article["tags"] = []
        return
    article["category_confidence"] = triage["category_confidence"]
    if article["category"] == typesafe_triage.OTHER_CATEGORY:
        # 見直しの材料として、採用しなかった第一候補を残す
        article["category_candidate"] = triage["category"]
    article["ai_prob"] = triage["ai_prob"]
    article["importance"] = triage["importance"]
    article["importance_detail"] = triage["scores"]
    article["tags"] = triage["tags"]

# ─── メイン処理 ──────────────────────────────────────────────────────────────

def main():
    cutoff = get_cutoff()
    today_jst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y年%m月%d日")
    summary_concurrency = get_summary_concurrency()
    print(f"=== Daily AI News 取得開始 ===")
    print(f"対象日: {today_jst}")
    print(f"取得期間: {cutoff.strftime('%Y-%m-%d %H:%M UTC')} 以降")
    print(f"フィード数: {sum(len(v) for v in FEED_CATEGORIES.values())}件")
    print(f"要約並列度: {summary_concurrency}")
    print(f"TypeSafe判定: {'有効' if typesafe_triage.is_enabled() else '無効（LLMのみで判定）'}")
    print()

    all_articles_flat = []  # 全カテゴリをまたいだ重複排除用
    fetched_count = 0

    # ── Step 1〜2: 取得・タイトル重複排除 ──
    for category, feeds in FEED_CATEGORIES.items():
        print(f"[{category}]")
        raw_articles = []

        for feed_name, url in feeds:
            feed = fetch_feed(feed_name, url)
            if not feed:
                continue
            for entry in feed.entries:
                t = parse_time(entry)
                if t and t >= cutoff:
                    raw_articles.append({
                        "source": feed_name,
                        "feed_group": category,
                        "title": getattr(entry, 'title', '（タイトルなし）'),
                        "url": getattr(entry, 'link', ''),
                        "date": format_jst(t),
                        "date_raw": t.isoformat(),
                        "text": get_text(entry),
                    })

        # タイトルベースの重複排除（カテゴリ内）
        deduped = deduplicate_by_title(raw_articles)
        fetched_count += len(raw_articles)
        print(f"  {len(raw_articles)}件取得 → タイトル重複排除後 {len(deduped)}件")

        all_articles_flat.extend(deduped)
        print()

    print(f"全カテゴリ合計: {len(all_articles_flat)}件")
    print()

    print("=== Step 3: サマリー生成・AI関連判定 ===")
    after_filter = []
    rejected = []
    route_counts = {"accept": 0, "reject": 0, "uncertain": 0, "fallback": 0}
    total_to_process = len(all_articles_flat)
    completed = 0
    with ThreadPoolExecutor(max_workers=summary_concurrency) as executor:
        future_to_index = {
            executor.submit(process_article, index, art): index
            for index, art in enumerate(all_articles_flat, start=1)
        }

        for future in as_completed(future_to_index):
            index, analysis = future.result()
            art = all_articles_flat[index - 1]
            art["summary"] = analysis["summary"]
            art["reason"] = analysis["reason"]
            apply_triage(art, analysis["triage"])
            route_counts[analysis["route"]] += 1

            completed += 1
            print(f"  [{log_now()}] [DONE {completed}/{total_to_process}] {art['title'][:60]}...")

            if analysis["is_ai_related"]:
                after_filter.append(art)
            else:
                rejected.append(art)
                print(f"  [{log_now()}] [AI関連フィルタ] 除外: {art['title'][:50]} / {analysis['reason']}")

    after_filter.sort(key=lambda art: art["date_raw"], reverse=True)
    print(f"サマリー生成・判定完了: {len(all_articles_flat)}件")
    print()

    after_dedup = all_articles_flat
    print("=== Step 4: AI関連フィルタ結果 ===")
    removed_filter = len(all_articles_flat) - len(after_filter)
    print(
        f"TypeSafe振り分け: 採用確定 {route_counts['accept']}件 / 除外確定 {route_counts['reject']}件 / "
        f"LLM判定 {route_counts['uncertain']}件 / フォールバック {route_counts['fallback']}件"
    )
    print(f"AI関連フィルタ: {len(all_articles_flat)}件 → {len(after_filter)}件（{removed_filter}件除去）")
    print()

    # ── 内容ベースのカテゴリ別にまとめる ──
    categorized: dict[str, list] = {cat: [] for cat in typesafe_triage.CATEGORY_ORDER}
    for art in after_filter:
        categorized[art["category"]].append(art)

    total = len(after_filter)
    print(f"最終合計: {total}件")
    for cat, arts in categorized.items():
        print(f"  {cat}: {len(arts)}件")

    output = {
        "date": today_jst,
        "date_slug": (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d"),
        "generated_at": (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M JST"),
        "total": total,
        "stats": {
            "fetched": fetched_count,
            "after_title_dedup": len(all_articles_flat),
            "after_dedup": len(after_dedup),
            "after_ai_filter": total,
            "triage_accepted": route_counts["accept"],
            "triage_rejected": route_counts["reject"],
            "triage_uncertain": route_counts["uncertain"],
            "triage_fallback": route_counts["fallback"],
            "typesafe_input_tokens": typesafe_triage.usage["input_tokens"],
            "category_other": len(categorized[typesafe_triage.OTHER_CATEGORY]),
        },
        "categories": categorized,
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 除外記事は判定評価（eval_typesafe.py）用に残す
    with REJECTED_JSON.open("w", encoding="utf-8") as f:
        json.dump({"date_slug": output["date_slug"], "articles": rejected}, f, ensure_ascii=False, indent=2)

    print(f"保存完了: {OUTPUT_JSON}")
    return output

if __name__ == "__main__":
    main()
