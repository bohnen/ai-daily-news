#!/usr/bin/env python3
"""Reproducible Daily AI News publisher.

LLM work stays in the existing generator. This program owns deterministic
preflight, validation, archive, Git, push, and draft-PR steps.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import dotenv_values
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "daily-ai-news-generator"
ENV_PATH = GENERATOR / "llm.env"
SECRETS_PATH = GENERATOR / "secrets.env"
OUTPUT = GENERATOR / "output" / "daily_articles.json"
DOCS = ROOT / "docs"


class Article(BaseModel):
    summary: str = ""
    reason: str = ""
    is_duplicate_candidate: bool = False
    ai_prob: float | None = None
    importance: float | None = None
    tags: list[str] = Field(default_factory=list)


class Stats(BaseModel):
    fetched: int = 0
    after_ai_filter: int = 0
    duplicate_candidates: int = 0
    visible_after_summary_dedup: int | None = None
    triage_accepted: int = 0
    triage_rejected: int = 0
    triage_uncertain: int = 0
    triage_fallback: int = 0
    typesafe_input_tokens: int = 0
    category_other: int = 0


class Edition(BaseModel):
    total: int
    stats: Stats = Field(default_factory=Stats)
    categories: dict[str, list[Article]] = Field(default_factory=dict)


class Result(BaseModel):
    date: str
    fetched: int
    after_ai_filter: int
    visible: int
    duplicate_candidates: int
    categories: dict[str, dict[str, int]]
    branch: str | None = None
    base_branch: str | None = None
    commit: str | None = None
    pull_request: str | None = None


def command(*args: str, capture: bool = False) -> str:
    print("+", " ".join(args), flush=True)
    completed = subprocess.run(args, cwd=ROOT, check=True, text=True, capture_output=capture)
    return completed.stdout.strip() if capture else ""


def today_jst() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d")


def config() -> dict[str, str]:
    # Same precedence as the generator: exported variables, then llm.env, then secrets.env.
    values = {**dotenv_values(SECRETS_PATH), **dotenv_values(ENV_PATH), **os.environ}
    values = {key: value for key, value in values.items() if value}
    if not ENV_PATH.is_file() or not values.get("LLM_BASE_URL") or not values.get("LLM_MODEL"):
        raise RuntimeError(f"invalid LLM configuration: {ENV_PATH}")
    return values


def check_llm(values: dict[str, str]) -> None:
    headers = {"Authorization": f"Bearer {values['LLM_API_KEY']}"} if values.get("LLM_API_KEY") else {}
    response = requests.get(f"{values['LLM_BASE_URL'].rstrip('/')}/models", headers=headers, timeout=30)
    response.raise_for_status()
    models = {model.get("id") for model in response.json().get("data", [])}
    if values["LLM_MODEL"] not in models:
        raise RuntimeError(f"LLM model not available: {values['LLM_MODEL']}")
    print(f"LLM endpoint ready: {values['LLM_MODEL']}")


def clean_worktree() -> None:
    if command("git", "status", "--porcelain", capture=True):
        raise RuntimeError("working tree has local changes; refusing to overwrite them")


def archive(ref: str) -> list[str]:
    return json.loads(command("git", "show", f"{ref}:docs/archive-index.json", capture=True))


def confirm_missing_previous(previous: str) -> None:
    prompt = (
        f"origin/main に前日分 ({previous}) の出力がありません。"
        "単にスキップされた可能性があります。"
        "main または最新の公開ブランチを基に続行しますか？ [y/N]: "
    )
    try:
        answer = input(prompt)
    except EOFError as error:
        raise RuntimeError("前日分の出力がないため、確認できず中止しました") from error
    if answer.strip().lower() not in {"y", "yes"}:
        raise RuntimeError("前日分の出力がないため、ユーザーの確認により中止しました")


def base_branch(date: str) -> str:
    previous = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    if previous in archive("origin/main"):
        return "main"
    confirm_missing_previous(previous)
    refs = command("git", "ls-remote", "--heads", "origin", "refs/heads/automation/daily-ai-news-publish-*", capture=True).splitlines()
    candidates = [line.rsplit("/", 1)[-1] for line in refs]
    candidates = [name for name in candidates if name < f"automation/daily-ai-news-publish-{date}"]
    if not candidates:
        print(f"前日分 ({previous}) の公開ブランチがないため、mainをベースにします", flush=True)
        return "main"
    return max(candidates)


def prepare_branch(date: str) -> tuple[str, str]:
    clean_worktree()
    command("git", "switch", "main")
    command("git", "pull", "--ff-only", "origin", "main")
    command("git", "fetch", "origin", "main")
    base = base_branch(date)
    branch = f"automation/daily-ai-news-publish-{date}"
    if command("git", "branch", "--list", branch, capture=True):
        raise RuntimeError(f"branch already exists: {branch}")
    command("git", "switch", "-c", branch, f"origin/{base}")
    return branch, base


def validate(date: str) -> Result:
    if not OUTPUT.is_file():
        raise RuntimeError(f"missing generator output: {OUTPUT}")
    edition = Edition.model_validate_json(OUTPUT.read_text(encoding="utf-8"))
    visible = edition.stats.visible_after_summary_dedup
    if visible is None:
        visible = edition.total - edition.stats.duplicate_candidates
    failures = [a for items in edition.categories.values() for a in items if "サマリー生成に失敗しました" in a.summary or "AI関連判定に失敗" in a.reason]
    html = DOCS / f"{date}.html"
    if edition.total <= 0 or visible <= 0 or failures or not html.is_file() or not html.stat().st_size:
        raise RuntimeError("validation failed: zero articles, failed LLM output, or missing HTML")
    categories = {}
    for name, items in edition.categories.items():
        duplicates = sum(item.is_duplicate_candidate for item in items)
        categories[name] = {"total": len(items), "visible": len(items) - duplicates, "duplicates": duplicates}
    return Result(date=date, fetched=edition.stats.fetched, after_ai_filter=edition.stats.after_ai_filter or edition.total, visible=visible, duplicate_candidates=edition.stats.duplicate_candidates, categories=categories)


def update_archive(date: str) -> None:
    command(sys.executable, str(GENERATOR / "scripts" / "push_to_github.py"), "--date", date, "--html", str(DOCS / f"{date}.html"))
    if json.loads((DOCS / "archive-index.json").read_text(encoding="utf-8"))[0] != date:
        raise RuntimeError("archive index does not start with the edition date")


def commit_and_pr(result: Result, base: str) -> Result:
    html = f"docs/{result.date}.html"
    command("git", "add", html, "docs/archive-index.json")
    staged = command("git", "diff", "--cached", "--name-only", capture=True).splitlines()
    if not staged or not set(staged).issubset({html, "docs/archive-index.json"}):
        raise RuntimeError(f"unexpected staged files: {staged}")
    command("git", "commit", "-m", f"Publish Daily AI News {result.date}")
    result.commit = command("git", "rev-parse", "HEAD", capture=True)
    result.branch, result.base_branch = command("git", "branch", "--show-current", capture=True), base
    command("git", "push", "-u", "origin", result.branch)
    body = f"## Daily AI News — {result.date}\n\n- Fetched: {result.fetched}\n- After AI filter: {result.after_ai_filter}\n- Visible: {result.visible}\n- Duplicate candidates: {result.duplicate_candidates}\n"
    result.pull_request = command("gh", "pr", "create", "--draft", "--base", base, "--head", result.branch, "--title", f"Publish Daily AI News {result.date}", "--body", body, capture=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=today_jst())
    parser.add_argument("--publish", action="store_true", help="prepare branch, commit, push, and create a draft PR")
    parser.add_argument("--skip-llm-check", action="store_true")
    args = parser.parse_args()
    datetime.strptime(args.date, "%Y-%m-%d")
    branch = base = None
    if args.publish:
        branch, base = prepare_branch(args.date)
    if not args.skip_llm_check:
        check_llm(config())
    command(str(GENERATOR / "scripts" / "run_daily_to_html.sh"))
    result = validate(args.date)
    update_archive(args.date)
    if args.publish:
        result = commit_and_pr(result, base or "main")
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, requests.RequestException, ValueError) as error:
        print(f"publish failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
