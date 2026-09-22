#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
ENV_PATH="${REPO_ROOT}/daily-ai-news-generator/llm.env"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Python executable not found: ${PYTHON_BIN}" >&2
  echo "Run 'uv sync' first." >&2
  exit 1
fi

if [[ ! -f "${ENV_PATH}" ]]; then
  echo "LLM environment file not found: ${ENV_PATH}" >&2
  exit 1
fi

# llm.env / secrets.env are loaded by the Python scripts (exported variables win).

timestamp() {
  date '+%Y-%m-%d %H:%M:%S %Z'
}

echo "[$(timestamp)] Daily AI News pipeline started"
echo "[$(timestamp)] Step 1/3: fetch + summarize"
"${PYTHON_BIN}" -u "${REPO_ROOT}/daily-ai-news-generator/scripts/fetch_daily.py"

echo "[$(timestamp)] Step 2/3: deduplicate by summary similarity"
"${PYTHON_BIN}" -u "${REPO_ROOT}/daily-ai-news-generator/scripts/deduplicate_by_summary.py"

echo "[$(timestamp)] Step 3/3: generate HTML"
"${PYTHON_BIN}" "${REPO_ROOT}/daily-ai-news-generator/scripts/generate_html.py"

echo "[$(timestamp)] Daily AI News pipeline finished"
