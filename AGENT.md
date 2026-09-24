# Agent Notes

## Repository Purpose

- This repository publishes the `Daily AI News` GitHub Pages site.
- Public pages are served from `docs/`.
- The project also contains generator scripts under `daily-ai-news-generator/` so daily content can be rebuilt and republished from the same repository.

## Published Output

- `docs/index.html`: archive landing page. It renders the archive as a month-by-month calendar view and links to each daily edition.
- `docs/YYYY-MM-DD.html`: one generated page per day.
- `docs/archive-index.json`: list of published dates used by `docs/index.html`.

## Generator Scripts

- `daily-ai-news-generator/scripts/fetch_daily.py`
  - Fetches recent items from 44 RSS feeds.
  - Deduplicates by title similarity.
  - Uses an OpenAI-compatible API to generate Japanese summaries.
  - Uses the same OpenAI-compatible API for summary generation and AI relevance filtering.
  - Before summarizing, calls `typesafe_triage.triage()` once per article: clear non-AI articles (`noul <= 0.3`) are dropped without an LLM call, clear AI articles (`>= 0.7`) skip the LLM's relevance verdict, and the rest are judged by the LLM as before. The same request yields `importance`, `tags`, and the article's category.
  - Categories are topic-based and come from TypeSafe, not from the feed: `typesafe_triage.CATEGORIES` (6 topics) plus `その他`. An article goes to `その他` when the choice confidence is below `CATEGORY_MIN_CONFIDENCE` (0.5) or TypeSafe is unavailable; `stats.category_other` tracks the count, and a rising share is the signal to revisit the taxonomy. The keys of `FEED_CATEGORIES` are only feed groups (`feed_group`).
  - Writes excluded articles to `daily-ai-news-generator/output/rejected_articles.json` for evaluation.
  - Writes `daily-ai-news-generator/output/daily_articles.json`.
- `daily-ai-news-generator/scripts/generate_html.py`
  - Reads `daily_articles.json`.
  - Generates `docs/YYYY-MM-DD.html`.
  - Generated daily pages include local-only saved state UI using `localStorage` with labels `保存` / `保存済み`.
- `daily-ai-news-generator/scripts/deduplicate_by_summary.py`
  - Reads `daily_articles.json` after summary generation.
  - Embeds the original title/text through the LLM endpoint's `/embeddings` (`SUMMARY_DEDUP_MODEL`, default `qwen/qwen3-embedding-8b` on Nous Portal) to detect near-duplicate articles. No local model.
  - Keeps the article with the longest summary as the representative within each similar cluster.
  - Marks the remaining articles as duplicate candidates instead of deleting them from JSON.
- `daily-ai-news-generator/scripts/push_to_github.py`
  - Updates `docs/archive-index.json`.
  - Ensures the generated daily HTML is in the expected `docs/` location.
  - Does not push by itself; git commit/push is handled separately.
- `daily-ai-news-generator/scripts/serve_docs.py`
  - Runs a simple local server for `docs/`.
  - Use this when `file://` access breaks `fetch('archive-index.json')` on the archive page.

## Python Environment

- Python is managed with `uv`.
- Project metadata lives in `pyproject.toml`.
- Python version target is recorded in `.python-version`.
- Typical setup:

```bash
uv sync
```

- Typical execution:

```bash
uv run python daily-ai-news-generator/scripts/fetch_daily.py
uv run python daily-ai-news-generator/scripts/deduplicate_by_summary.py
uv run python daily-ai-news-generator/scripts/generate_html.py
uv run python daily-ai-news-generator/scripts/push_to_github.py --date YYYY-MM-DD --html docs/YYYY-MM-DD.html
```

- To run the fetch and HTML generation pipeline in one command:

```bash
./daily-ai-news-generator/scripts/run_daily_to_html.sh
```

- For real-time progress logs from `fetch_daily.py`, prefer unbuffered execution:

```bash
.venv/bin/python -u daily-ai-news-generator/scripts/fetch_daily.py
```

- Local preview:

```bash
uv run python daily-ai-news-generator/scripts/serve_docs.py
```

## Secrets And Environment Files

- `daily-ai-news-generator/scripts/fetch_daily.py` loads `daily-ai-news-generator/llm.env` and then the untracked `daily-ai-news-generator/secrets.env` via `python-dotenv`. Already-exported environment variables win.
- LLM access uses an OpenAI-compatible Chat Completions API configured by `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY`.
- The default configuration is Nous Portal (`https://inference-api.nousresearch.com/v1`) with `deepseek/deepseek-v4-flash`. `LLM_API_KEY` is required unless `LLM_BASE_URL` points at localhost.
- Requests send `strict` JSON schema and, with `LLM_DISABLE_REASONING=1`, `reasoning: {enabled: false}`; without these the routed provider may drop required keys or burn the token budget on reasoning.
- Summary parallelism is controlled by `SUMMARY_CONCURRENCY` and defaults to `3`.
- `llm.env` sets `SUMMARY_CONCURRENCY=5` for the remote API; lower it if the provider returns 429s (requests retry with backoff).
- TypeSafe (`TYPESAFE_API_KEY`) is optional and fail-open: without the key or on API failure every script behaves as it did before TypeSafe. Thresholds, the tag vocabulary, and importance weights are constants in `daily-ai-news-generator/scripts/typesafe_triage.py`; check changes with `uv run python daily-ai-news-generator/scripts/eval_typesafe.py` (read-only against `output/`).
- `importance` exists to surface technical / implementation information for engineers, not business impact: it combines `practical_value`, `technical_depth`, and technical `novelty` (rubrics and weights in `typesafe_triage.py`). Do not add a business-impact score.
- The AI-relevance reject threshold stays at `0.3`; the owner prefers fewer articles over recall.
- TypeSafe cannot generate text and is weak at date comparison and counting; use it only for yes/no, choice, and rubric-score judgments, and pass it the original English title/text rather than the Japanese summary.
- Pure-function tests: `uv run --with pytest pytest daily-ai-news-generator/tests`.
- Summary-level deduplication uses `SUMMARY_DEDUP_MODEL` and `SUMMARY_DEDUP_THRESHOLD`; defaults are `qwen/qwen3-embedding-8b` and `0.78`. Pairs with similarity in `[0.65, 0.90)` are confirmed by TypeSafe (`same_event`) when the key is set; `>= 0.90` is a duplicate outright. The thresholds are calibrated to this model on original English text; re-measure them (`DEDUP_LOW`/`DEDUP_HIGH` in `deduplicate_by_summary.py`) if the model changes.
- `daily-ai-news-generator/llm.env` is intentionally tracked and must not contain credentials. Put `LLM_API_KEY` and `TYPESAFE_API_KEY` in the gitignored `secrets.env` (template: `secrets.env.example`) or the environment, and never print their values.
- When working in a worktree or publish clone, `secrets.env` is not present; export the keys or copy the file.

## Daily Publish Workflow

- Use `uv run python ...` or `.venv/bin/python ...`; do not rely on a bare `python` executable being available.
- The generation worktree may be detached and may have a stale or noncanonical `docs/archive-index.json`.
- Generate the daily HTML in the active worktree, but create the publish commit from a clean temporary clone when the active worktree is detached or has unreliable git metadata.
- Choose the publish base from remote refs. Use `origin/main` only when it already contains the prior published archive entries; otherwise base the commit on the latest previous `origin/automation/daily-ai-news-publish-YYYY-MM-DD` branch.
- Prefer `git ls-remote` for remote ref checks in detached worktrees because `git fetch` can fail when linked worktree metadata is not writable.
- In the publish clone, copy in only `docs/YYYY-MM-DD.html`, rerun `daily-ai-news-generator/scripts/push_to_github.py`, then verify that `docs/archive-index.json` starts with the new date and still includes recent prior dates.
- Commit only `docs/YYYY-MM-DD.html` and `docs/archive-index.json` for daily publish branches.
- Do not commit `.venv` or `daily-ai-news-generator/output/`.

## Operational Expectations

- Do not publish or commit a zero-article daily edition caused by network failure or LLM failure.
- Validate `daily-ai-news-generator/output/daily_articles.json` before publishing. Confirm nonzero AI-filtered total, nonzero visible published count, duplicate-candidate count, and category breakdown.
- When updating daily content, verify that `docs/archive-index.json` and the target daily page remain in a coherent published state.
- Keep generated intermediate files out of git; `daily-ai-news-generator/output/` is intentionally ignored.
