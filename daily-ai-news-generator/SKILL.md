---
name: daily-ai-news-generator
description: 45本のRSSフィードから過去24時間のAIニュースを収集・要約・重複排除・AI関連フィルタリングして、このリポジトリの`docs/`に日次HTMLを生成するCodex用スキル。「Daily AI Newsを更新して」「今日のAIニュースを公開して」などの依頼や、Codexオートメーションでの日次更新時に使用する。
---

# Daily AI News Generator

45本のRSSフィードから過去24時間のAIニュースを収集し、日本語サマリー付きHTMLを生成して、このリポジトリのGitHub Pages用`docs/`を更新する。

## 使う場面

- `ai-daily-news` リポジトリを日次更新するとき
- `docs/YYYY-MM-DD.html` と `docs/archive-index.json` を更新するとき
- Codexオートメーションで定期実行するとき

## 前提

- カレントワーキングディレクトリはリポジトリルートにする
- LLM は OpenAI 互換 Chat Completions API を使う（デフォルト: Nous Portal の `deepseek/deepseek-v4-flash`）
- `daily-ai-news-generator/llm.env` に `LLM_BASE_URL`、`LLM_MODEL` を設定しておく
- API キー（`LLM_API_KEY`、任意で `TYPESAFE_API_KEY`）は環境変数か、git 管理外の `daily-ai-news-generator/secrets.env` で渡す（雛形: `secrets.env.example`）。export 済みの環境変数が優先される
- 要約の並列度は `daily-ai-news-generator/llm.env` の `SUMMARY_CONCURRENCY` で調整し、未設定時は `3` を使う
- 要約・判定リクエストの生成トークン上限は `SUMMARY_MAX_OUTPUT_TOKENS` で調整し、未設定時は `500` を使う
- `git` でコミット・プッシュできる状態が望ましい。ただし Codex オートメーションでは detached worktree のことがあるため、公開コミットは必要に応じて一時 clone で作成する
- Python環境は `uv` で管理する

```bash
uv sync
```

## 基本手順

### 1. 環境確認

```bash
if [ ! -f daily-ai-news-generator/llm.env ]; then
  echo "daily-ai-news-generator/llm.env is missing" >&2
  exit 1
fi
set -a
source daily-ai-news-generator/llm.env
[ -f daily-ai-news-generator/secrets.env ] && source daily-ai-news-generator/secrets.env
set +a
test -n "${LLM_BASE_URL:-}"
test -n "${LLM_MODEL:-}"
test -n "${LLM_API_KEY:-}"
export UV_CACHE_DIR=/tmp/uv-cache
uv sync
```

`daily-ai-news-generator/llm.env` がない場合、または `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` が空の場合は公開せずに停止する。`SUMMARY_CONCURRENCY`、`SUMMARY_MAX_OUTPUT_TOKENS`、`SUMMARY_DEDUP_MODEL`、`SUMMARY_DEDUP_THRESHOLD` は `llm.env` の値を使い、オートメーション側で上書きしない。

現状の生成処理は `fetch_daily.py` のサマリー生成とAI関連判定で、OpenAI 互換 Chat Completions API を使う。`llm.env` はクレデンシャルを含まないコピー可能な設定として git 管理する。秘密値は `llm.env` に入れず、環境変数か `secrets.env`（git 管理外）で渡す。値をログや出力に表示しない。

### 2. 生成パイプライン

```bash
./daily-ai-news-generator/scripts/run_daily_to_html.sh
```

このスクリプトは次を順番に実行する。
- `fetch_daily.py`: 記事取得、タイトル重複排除、サマリー生成、AI関連フィルタリング
- `deduplicate_by_summary.py`: サマリー類似度による重複候補注釈
- `generate_html.py`: `docs/YYYY-MM-DD.html` の生成

出力:
- `daily-ai-news-generator/output/daily_articles.json`
- `docs/YYYY-MM-DD.html`

個別に実行する場合:

```bash
uv run python daily-ai-news-generator/scripts/fetch_daily.py
uv run python daily-ai-news-generator/scripts/deduplicate_by_summary.py
uv run python daily-ai-news-generator/scripts/generate_html.py
```

`fetch_daily.py` と `deduplicate_by_summary.py` は `daily-ai-news-generator/llm.env` と `secrets.env` を直接読み込む（export 済みの環境変数は上書きしない）。`run_daily_to_html.sh` は `llm.env` の存在だけを確認する。

補足:
- `sentence-transformers` と `hotchpotch/static-embedding-japanese` を使ってサマリー同士の近似重複を検出する
- 複数の類似記事がある場合は、最もサマリーが長い記事を代表記事として残す
- 代表以外の記事は JSON に残したまま重複候補としてマークし、HTML ではデフォルト非表示にする

### 3. JSON と HTML の検証

```bash
export DATE=$(uv run python -c "from datetime import datetime, timezone, timedelta; print((datetime.now(timezone.utc)+timedelta(hours=9)).strftime('%Y-%m-%d'))")
uv run python - <<'PY'
import json
from pathlib import Path

data = json.loads(Path("daily-ai-news-generator/output/daily_articles.json").read_text(encoding="utf-8"))
stats = data.get("stats", {})
total = int(data.get("total", 0))
duplicates = int(stats.get("duplicate_candidates", 0))
visible = int(stats.get("visible_after_summary_dedup", total - duplicates))
print(f"total_after_ai_filter={total}")
print(f"duplicate_candidates={duplicates}")
print(f"visible={visible}")
for category, articles in data.get("categories", {}).items():
    cat_visible = sum(1 for article in articles if not article.get("is_duplicate_candidate"))
    cat_duplicates = sum(1 for article in articles if article.get("is_duplicate_candidate"))
    print(f"{category}\tvisible={cat_visible}\ttotal={len(articles)}\tdup={cat_duplicates}")
if total <= 0 or visible <= 0:
    raise SystemExit("refusing to publish zero-visible daily edition")
PY
test -s "docs/$DATE.html"
```

ゼロ記事版を公開しない。欠落シークレット、API失敗、全件重複候補化などで `visible` が 0 になった場合は停止する。

### 4. 公開用 clone でアーカイブ更新

```bash
export DATE=$(uv run python -c "from datetime import datetime, timezone, timedelta; print((datetime.now(timezone.utc)+timedelta(hours=9)).strftime('%Y-%m-%d'))")
SRC=$(pwd)
TMP="/private/tmp/ai-daily-news-publish-${DATE}-$$"
BRANCH="automation/daily-ai-news-publish-${DATE}"

# origin/main が前日以前の archive をすべて含むなら BASE_BRANCH=main を使う。
# 未マージの日次 automation branch がある場合は、最新の前日 automation branch 名を使う。
BASE_BRANCH="automation/daily-ai-news-publish-YYYY-MM-DD"
git clone --single-branch --branch "$BASE_BRANCH" git@github-tadapin:tadapin/ai-daily-news.git "$TMP"
cd "$TMP"
git switch -c "$BRANCH"
cp "$SRC/daily-ai-news-generator/llm.env" daily-ai-news-generator/llm.env
cp "$SRC/docs/$DATE.html" "docs/$DATE.html"
set -a
source daily-ai-news-generator/llm.env
# secrets.env は clone にコピーしない（誤コミット防止）。元の場所から読む。
[ -f "$SRC/daily-ai-news-generator/secrets.env" ] && source "$SRC/daily-ai-news-generator/secrets.env"
set +a
export UV_CACHE_DIR=/tmp/uv-cache
uv run python daily-ai-news-generator/scripts/push_to_github.py --date "$DATE" --html "docs/$DATE.html"
```

detached worktree で生成された `docs/archive-index.json` は canonical でない場合があるため、公開コミットでは信用しない。公開用 clone 側で `push_to_github.py` を再実行し、最新日と前日以前の archive が残っていることを確認する。

base branch の選び方:
- `git ls-remote --heads origin refs/heads/main 'refs/heads/automation/daily-ai-news-publish-*'` で remote refs を確認する
- `origin/main` に前日以前の `archive-index.json` が含まれていれば `main` を使う
- `origin/main` が未マージの日次分を欠く場合は、直近の `origin/automation/daily-ai-news-publish-YYYY-MM-DD` を使う
- detached worktree では `FETCH_HEAD` 書き込みに失敗することがあるため、remote 確認には `git ls-remote` を優先する

### 5. 公開状態の検証と Git 反映

```bash
uv run python - <<'PY'
import json
import os
from pathlib import Path

date = os.environ["DATE"]
index = json.loads(Path("docs/archive-index.json").read_text(encoding="utf-8"))
print(index[:8])
if not index:
    raise SystemExit("archive-index.json is empty")
if index[0] != date:
    raise SystemExit(f"archive-index.json does not start with {date}")
if not Path(f"docs/{date}.html").is_file():
    raise SystemExit(f"docs/{date}.html is missing")
PY
test -s "docs/$DATE.html"
git status --short
git add "docs/$DATE.html" docs/archive-index.json
git commit -m "Publish Daily AI News $DATE"
git push -u origin "$BRANCH"
git ls-remote --heads origin "refs/heads/$BRANCH"
```

コミット対象は通常 `docs/$DATE.html` と `docs/archive-index.json` のみ。`.venv`、`daily-ai-news-generator/output/`、生成元 detached worktree に残った非canonicalな `docs/archive-index.json` はコミットしない。

## 報告内容

実行後は次を簡潔に報告する。
- 取得件数
- AI関連フィルタ後の件数
- visible published count
- duplicate-candidate count
- カテゴリ別内訳
- 更新したファイル
- commit hash
- pushed branch/ref
- detached worktree や一時 clone などのローカル作業上の注意
- 必要なら公開URL

公開URL:
- トップページ: https://tadapin.github.io/ai-daily-news/
- 当日号: https://tadapin.github.io/ai-daily-news/YYYY-MM-DD.html

## スクリプトの処理フロー

`fetch_daily.py` の処理：
1. 44フィードから過去24時間の記事を取得
2. タイトルベースの重複排除（SequenceMatcher、閾値0.75）
3. LLM（OpenAI互換Chat Completions）で日本語サマリー生成（2〜3文、十分な長さ）
4. LLM（OpenAI互換Chat Completions）で AI 関連フィルタリング（AI/ML/LLM/自動化に無関係な記事を除去）
5. `daily-ai-news-generator/output/daily_articles.json` に出力

`deduplicate_by_summary.py` の処理：
- サマリー埋め込みの cosine 類似度で近似重複記事をクラスタ化
- クラスタ内では最長サマリーの記事を代表記事として採用
- 代表以外には重複候補フラグ、代表記事ID、類似度を付与する
- `daily_articles.json` を上書き更新

`generate_html.py` の処理：
- `daily_articles.json` を読み込みダークテーマのHTMLを生成
- 左サイドバー（カテゴリナビ・統計）＋右カードグリッドのレイアウト
- 出力: `docs/YYYY-MM-DD.html`

`push_to_github.py` の処理：
- `docs/archive-index.json` に対象日を追加
- 必要なら指定HTMLを `docs/YYYY-MM-DD.html` にそろえる
- Git操作自体は行わず、Codexまたはオートメーション本体に委ねる

## 記事カテゴリ

記事のカテゴリはフィードではなく内容で決まる（TypeSafe の choice 判定、定義は `scripts/typesafe_triage.py` の `CATEGORIES`）。
モデル・研究 / 製品・サービス / 開発・エンジニアリング / ビジネス・業界 / 安全・セキュリティ / 政策・社会 の6分類に加え、
confidence が 0.5 未満の記事と TypeSafe が使えないときの記事は「その他」に入る。
`stats.category_other` が増えてきたらカテゴリ体系の見直しを検討する。

## フィード一覧（44件）

下表の「グループ」はフィードの束ね方（記事の `feed_group`）で、記事カテゴリとは別。

| グループ | 件数 | 主な情報源 |
|---|---|---|
| Anthropic | 6件 | Anthropic News/Research/Engineering、Claude Code Changelog等 |
| AI開発ツール | 5件 | Cursor、Ollama、Windsurf Blog/Changelog等 |
| AIベンダー | 15件 | OpenAI、Google DeepMind、Microsoft AI、AWS ML Blog、Hugging Face、NVIDIA等 |
| AIニュース・メディア | 8件 | MIT Technology Review、VentureBeat、TechCrunch AI、WIRED AI、The Verge、AI News等 |
| 研究者・ニュースレター | 10件 | Import AI、Ahead of AI、Simon Willison、Andrej Karpathy、Last Week in AI等 |
