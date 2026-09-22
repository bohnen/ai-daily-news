#!/usr/bin/env python3
"""
TypeSafe (System One / Jev) による記事の「判定」処理。

1記事 = 1リクエストに全質問を相乗りさせる（質問を足しても遅延はほぼ増えない）。
TYPESAFE_API_KEY が未設定、または API が失敗した場合は None を返す（fail-open）。
呼び出し側は None のとき従来の LLM 経路にフォールバックする。

注意: Jev は state を字義どおりに読む。instructions は英語の平叙文で書き、
state には英語が多い原文（title + text）を渡す。日付比較や数え上げは聞かない。
"""

import os
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / "daily-ai-news-generator" / "llm.env"
SECRETS_PATH = REPO_ROOT / "daily-ai-news-generator" / "secrets.env"

load_dotenv(ENV_PATH)
load_dotenv(SECRETS_PATH)

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")
TYPESAFE_RETRIES = 3

# ===== 閾値・重み（eval_typesafe.py の結果で調整する） =====
AI_ACCEPT = 0.7
AI_REJECT = 0.3
TAG_THRESHOLD = 0.7
CATEGORY_MIN_CONFIDENCE = 0.5
SAME_EVENT_THRESHOLD = 0.5
# 収集目的は技術・実装情報。ビジネス上の影響の大きさは重要度に入れない。
IMPORTANCE_WEIGHTS = {"practical_value": 0.4, "technical_depth": 0.4, "novelty": 0.2}

AI_QUESTION = {
    "type": "noul",
    "instructions": (
        "The main topic of this article is artificial intelligence, machine learning, "
        "large language models, robotics, or a product, company, research result or policy about them. "
        "AI being mentioned only in passing does not count."
    ),
}

SCORE_QUESTIONS = {
    "practical_value": {
        "type": "score",
        "instructions": "How directly useful this article is to a software engineer who builds applications with AI models",
        "criteria": [
            "Nothing an engineer can use: business, funding, politics, personnel, or general-interest news",
            "Background awareness only: an announcement or incident with no usable technical information",
            "Tells engineers that a tool, model, API, or feature exists or changed, without saying how to use it",
            "Explains how to use or apply a tool, model, API, or technique, with concrete specifics",
            "Directly actionable: working code, configuration, commands, measured results, or step-by-step implementation guidance",
        ],
    },
    "technical_depth": {
        "type": "score",
        "instructions": "How much technical detail this article contains",
        "criteria": [
            "No technical content",
            "Mentions technology only by name",
            "Explains how something works at a high level",
            "Gives concrete technical details such as methods, benchmarks, parameters, or code",
            "In-depth technical material such as a paper, architecture, or detailed engineering write-up",
        ],
    },
    "novelty": {
        "type": "score",
        "instructions": "How technically new the information in this article is",
        "criteria": [
            "Recap, roundup, opinion, or commentary on already known things",
            "Small incremental update such as a minor version or bug fixes",
            "A new feature, capability, or measured result",
            "A new model, tool, technique, or research direction",
            "A first-of-its-kind technical capability or breakthrough",
        ],
    },
}

# 「何の話か」の1軸で分ける。説明文は Jev が字義どおり読むので具体的に書く。
CATEGORIES = {
    "モデル・研究": "A new AI model or model version being released or becoming available, or a research paper, benchmark result, or scientific finding about AI",
    "製品・サービス": "A product, app, feature, or service for end users or businesses, including AI assistants and consumer agents",
    "開発・エンジニアリング": "Building software with AI: coding assistants, agent frameworks, APIs, SDKs, cloud ML platforms, inference serving, evaluation methods, changelogs, and engineering how-tos",
    "ビジネス・業界": "Company and market news: funding, acquisitions, valuations, partnerships, executive hires, enterprise adoption, chips and data center investment",
    "安全・セキュリティ": "AI safety and alignment, existential or catastrophic risk debate, hacking and security incidents involving AI, misuse, hallucination-caused harm",
    "政策・社会": "Government regulation, legislation, elections and lobbying, lawsuits, public-sector use of AI, and effects on jobs, education, media or culture",
}
# confidence が CATEGORY_MIN_CONFIDENCE 未満、または TypeSafe が使えないときの受け皿。
# ここが増えてきたらカテゴリ体系を見直すサイン（stats.category_other で追える）。
OTHER_CATEGORY = "その他"
CATEGORY_ORDER = [*CATEGORIES, OTHER_CATEGORY]

# slug -> (表示名, instructions)。タグは固定語彙のみ（Jev は自由生成できない）。
# 絞り込みに使えるよう、タグは具体的にする。「LLM」「製品」のように大半の記事に付く
# 広いタグは入れない（1タグが全体の2割を超えたら分割か削除を検討。eval_typesafe.py で確認）。
# 判定文は "centrally about" / "main subject" と書き、軽い言及では付かないようにする。
def _company(subject):
    return f"{subject} is a main subject of this article, not merely mentioned in passing"


TAGS = {
    # 企業・組織
    "anthropic": ("Anthropic", _company("Anthropic or its Claude models")),
    "openai": ("OpenAI", _company("OpenAI or ChatGPT")),
    "google": ("Google", _company("Google, Google DeepMind, or Gemini")),
    "meta": ("Meta", _company("Meta or its Llama models")),
    "microsoft": ("Microsoft", _company("Microsoft or Copilot")),
    "amazon": ("Amazon・AWS", _company("Amazon, AWS, Bedrock, or SageMaker")),
    "nvidia": ("NVIDIA", _company("NVIDIA")),
    "xai": ("xAI", _company("xAI, Grok, or Elon Musk's AI efforts")),
    "china_ai": ("中国AI", "A Chinese AI company or model such as DeepSeek, Qwen, Kimi, or Manus is a main subject of this article"),
    # モデル・研究
    "model_release": ("モデルリリース", "This article announces that a specific new AI model or model version is released or becomes available"),
    "open_weights": ("オープンウェイト", "This article is about a model whose weights are openly downloadable, or about open-source AI software releases"),
    "research_paper": ("論文・研究成果", "This article reports the findings of a specific research paper or scientific study"),
    "benchmark": ("評価・ベンチマーク", "This article is centrally about evaluating AI systems: evals methodology, benchmarks, or measured performance comparisons"),
    "training": ("学習・チューニング", "This article is centrally about how models are trained, fine-tuned, or improved with reinforcement learning"),
    # 開発
    "coding_agents": ("コーディングエージェント", "This article is centrally about AI tools that write or review software code, such as Claude Code, Cursor, Copilot, or Codex"),
    "agent_dev": ("エージェント開発", "This article is centrally about how to build, run, or orchestrate AI agents: frameworks, runtimes, tool use, MCP, or multi-agent design"),
    "inference": ("推論・サービング", "This article is centrally about serving models in production: inference speed, latency, cost, quantization, or deployment infrastructure"),
    "rag_data": ("データ・RAG", "This article is centrally about datasets, retrieval-augmented generation, embeddings, or vector search"),
    "tutorial": ("チュートリアル", "This article is a step-by-step tutorial or how-to guide"),
    "changelog": ("リリースノート", "This article is a changelog or release notes listing changes in a software version"),
    # 製品
    "consumer_agent": ("アシスタント・消費者向け", "This article is centrally about an AI assistant or agent product for consumers or office workers that takes actions for the user"),
    "gen_media": ("画像・動画・音声生成", "This article is centrally about AI that generates or edits images, video, music, or voice"),
    "robotics": ("ロボティクス", "This article is centrally about robots, autonomous vehicles, or physical AI"),
    # ビジネス
    "funding": ("資金調達・M&A", "This article is centrally about a funding round, acquisition, merger, IPO, or company valuation"),
    "people": ("人事・組織", "This article is centrally about an executive hire, departure, or organizational change at a company"),
    "chips_dc": ("半導体・データセンター", "This article is centrally about AI chips, GPUs, data centers, or the energy they consume"),
    # 安全
    "ai_hacking": ("AIによる攻撃・脆弱性", "This article is centrally about a cyberattack, hack, or software vulnerability in which an AI system was the attacker, the tool, or the target"),
    "xrisk": ("存亡リスク論争", "This article is centrally about the debate over whether AI could cause human extinction or catastrophe, or about pausing or slowing AI development"),
    "alignment": ("アライメント研究", "This article is centrally about technical research on AI alignment, interpretability, or model behavior evaluations for safety"),
    "hallucination": ("誤情報・ハルシネーション", "This article is centrally about AI systems producing false information and the harm it caused"),
    # 政策・社会
    "regulation": ("規制・立法", "This article is centrally about a specific law, bill, executive order, or regulator action on AI"),
    "politics": ("政治・ロビー活動", "This article is centrally about elections, political campaigns, lobbying, or political spending related to AI"),
    "legal": ("訴訟・著作権", "This article is centrally about a lawsuit, court ruling, or copyright dispute"),
    "military_gov": ("軍事・政府利用", "This article is centrally about military, intelligence, or government agencies using AI systems"),
    "jobs_edu": ("雇用・教育", "This article is centrally about AI's effect on jobs, careers, hiring, schools, or teaching"),
    "health_science": ("医療・科学", "This article is centrally about AI in medicine, biology, drug discovery, or natural science"),
    "media_culture": ("メディア・文化", "This article is centrally about AI's effect on the web, journalism, entertainment, or creative industries"),
}

_usage_lock = threading.Lock()
usage = {"requests": 0, "input_tokens": 0, "failures": 0}


def is_enabled():
    return bool(os.environ.get("TYPESAFE_API_KEY"))


def ask(state, questions):
    """TypeSafe に問い合わせて answers を返す。キー未設定・失敗時は None。"""
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        return None

    error = ""
    for attempt in range(1, TYPESAFE_RETRIES + 1):
        try:
            resp = requests.post(
                TYPESAFE_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"state": state, "model": TYPESAFE_MODEL, "questions": questions},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                with _usage_lock:
                    usage["requests"] += 1
                    usage["input_tokens"] += int((data.get("usage") or {}).get("input_tokens", 0))
                return data["answers"]
            error = f"HTTP {resp.status_code}"
            if resp.status_code not in (429, 529) and resp.status_code < 500:
                error = f"{error}: {resp.text[:200]}"
                break
        except (requests.RequestException, ValueError, KeyError) as e:
            error = str(e)
        if attempt < TYPESAFE_RETRIES:
            time.sleep(2 ** attempt)

    with _usage_lock:
        usage["failures"] += 1
    print(f"  [TYPESAFE ERROR] {error}")
    return None


def build_state(article):
    return (
        f"Source: {article.get('source', '')}\n"
        f"Title: {article.get('title', '')}\n\n"
        f"{article.get('text', '')}"
    )


def build_questions():
    questions = {"is_ai": AI_QUESTION, **SCORE_QUESTIONS}
    questions["category"] = {
        "type": "choice",
        "instructions": "Which single section of an AI news digest this article belongs in",
        "criteria": CATEGORIES,
    }
    for slug, (_, instructions) in TAGS.items():
        questions[f"tag_{slug}"] = {"type": "noul", "instructions": instructions}
    return questions


def compute_importance(scores):
    """0-4 の各スコアを重み付けして 0-1 に正規化する。"""
    return round(sum(IMPORTANCE_WEIGHTS[name] * scores[name] / 4 for name in IMPORTANCE_WEIGHTS), 3)


def resolve_category(triage):
    """確信のある choice だけ採用し、それ以外は「その他」に入れる。"""
    if (
        triage is not None
        and triage["category"] in CATEGORIES
        and triage["category_confidence"] >= CATEGORY_MIN_CONFIDENCE
    ):
        return triage["category"]
    return OTHER_CATEGORY


def route_ai(ai_prob):
    """'accept' / 'reject' / 'uncertain'。uncertain は LLM に判定させる。"""
    if ai_prob is None:
        return "uncertain"
    if ai_prob >= AI_ACCEPT:
        return "accept"
    if ai_prob <= AI_REJECT:
        return "reject"
    return "uncertain"


def triage(article):
    answers = ask(build_state(article), build_questions())
    if answers is None:
        return None
    try:
        scores = {name: float(answers[name]["score"]) for name in SCORE_QUESTIONS}
        tag_probs = {slug: float(answers[f"tag_{slug}"]["noul"]) for slug in TAGS}
        return {
            "ai_prob": float(answers["is_ai"]["noul"]),
            "scores": scores,
            "importance": compute_importance(scores),
            "category": answers["category"]["choice"],
            "category_confidence": float(answers["category"]["confidence"]),
            "tag_probs": tag_probs,
            "tags": [slug for slug, prob in tag_probs.items() if prob >= TAG_THRESHOLD],
        }
    except (KeyError, TypeError, ValueError) as e:
        print(f"  [TYPESAFE ERROR] unexpected answer shape: {e}")
        return None


def same_event(article_a, article_b):
    """2記事が同一のニュース事象を報じている確率。失敗時は None。"""
    state = (
        f"Article A\n{build_state(article_a)}\n\n"
        f"Article B\n{build_state(article_b)}"
    )
    answers = ask(state, {
        "same_event": {
            "type": "noul",
            "instructions": (
                "Article A and Article B report the same specific news event, "
                "such as the same announcement, release, deal, or incident. "
                "Being about the same company or the same general topic is not enough."
            ),
        },
    })
    if answers is None:
        return None
    try:
        return float(answers["same_event"]["noul"])
    except (KeyError, TypeError, ValueError):
        return None
