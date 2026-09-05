#!/usr/bin/env bash
# Drain the the lead inbox UI job ingestion queue once.
# Env (ANTHROPIC_API_KEY, DISCORD_WEBHOOK_URL, JOB_DB, JOB_INBOX_DIR) comes from
# the systemd EnvironmentFile / crontab environment.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
if [ -x "$REPO/.venv/bin/python" ]; then
  exec "$REPO/.venv/bin/python" job_ingest.py
fi
exec python3 job_ingest.py
