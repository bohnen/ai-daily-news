# Scheduled publisher

`scripts/publish_daily.py` is the deterministic entry point for one Daily AI
News edition. It keeps the existing LLM generator (OpenAI-compatible API), then validates its
JSON, updates the archive, and—when requested—commits, pushes, and opens a
draft pull request.

```bash
uv run python daily-ai-news-generator/scripts/publish_daily.py
uv run python daily-ai-news-generator/scripts/publish_daily.py --publish
```

The first command generates and validates an edition without GitHub writes.
`--publish` requires a clean worktree. It updates `main`, selects `main` or the
most recent daily-publish branch as the base, creates the daily branch, and
performs the publish steps. A failed LLM response, empty edition, or empty HTML
stops the job before any Git changes.

## macOS scheduling

The job needs `LLM_API_KEY` in `daily-ai-news-generator/secrets.env` (gitignored;
see `secrets.env.example`). Install the example as a user
LaunchAgent after reviewing its absolute repository path:

```bash
cp daily-ai-news-generator/launchd/com.tadapin.ai-daily-news.plist.example \
  ~/Library/LaunchAgents/com.tadapin.ai-daily-news.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.tadapin.ai-daily-news.plist
```

The template runs at 07:00 local time and logs to `/tmp/ai-daily-news-publish*.log`.
To run it immediately, use:

```bash
launchctl kickstart -k "gui/$(id -u)/com.tadapin.ai-daily-news"
```
