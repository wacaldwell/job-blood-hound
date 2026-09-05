#!/usr/bin/env bash
# Serve the job-hound write API on localhost for the lead inbox UI.
# Env (JOB_DB, JOB_API_TOKEN, JOB_API_PORT) comes from the systemd
# EnvironmentFile.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
PY="python3"
if [ -x "$REPO/.venv/bin/python" ]; then
  PY="$REPO/.venv/bin/python"
fi
exec "$PY" -m uvicorn jobapi:app \
  --host 127.0.0.1 --port "${JOB_API_PORT:-8765}"
