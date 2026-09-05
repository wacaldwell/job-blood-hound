#!/usr/bin/env bash
# daily.sh - unattended daily discovery + fit-ranking for job-hound.
# Runs on the always-on deployment host via cron. Posts a ranked digest to Discord.
#
# Required env (set in the host environment, never committed):
#   DISCORD_WEBHOOK_URL   incoming webhook for the digest (or set in companies.yaml)
#   JOB_DB                path to jobs.db (the single source of truth on this host)
#   JOB_APPS_DIR          application packages root
#
# Optional:
#   OPENJOBS_ENABLED=0    switch off the open-jobs wide net
#   OPENJOBS_TOP          new leads the wide net may ingest per run (default 15)
#   OPENJOBS_TIMEOUT      wall-clock cap on the wide net (default 10m)
#
# ANTHROPIC_API_KEY is deliberately not required here. The daily ranking path
# is deterministic and free; manual refine, gate, draft, and the lead inbox UI
# ingestion load the key through their own host environment.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

LOG_DIR="${LOG_DIR:-$HOME/logs}/job-hound"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/daily.log"

PY="${PY:-$HERE/.venv/bin/python}"

# --- weekly liveness sweep -------------------------------------------------
# `prune --apply` walks hundreds of other people's public ATS endpoints at the
# deliberate 1.5s politeness delay, so it takes 10 to 15 minutes on the current
# backlog. It runs one day a week, not daily: postings do not die fast enough
# to justify knocking on every endpoint every morning.
# PRUNE_DAY is `date +%u`, so 1=Monday through 7=Sunday. Sunday, because that
# is the quietest day on the endpoints we are walking and the day the digest
# can most afford to land 10 minutes late.
# Set PRUNE_ENABLED=0 to switch the sweep off without deleting it.
PRUNE_ENABLED="${PRUNE_ENABLED:-1}"
PRUNE_DAY="${PRUNE_DAY:-7}"
PRUNE_TIMEOUT="${PRUNE_TIMEOUT:-25m}"

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) daily run start ==="
  "$PY" job_cli.py scan
  # --- wide net ------------------------------------------------------------
  # Second discovery source: the open-jobs corpus (~2M postings, ~65,000
  # boards), capped at OPENJOBS_TOP new leads per run. No LLM call and no API
  # spend, so the digest stays deterministic and free.
  #
  # Isolated from `set -e` for the same reason the prune sweep is: the wide net
  # is a nice-to-have and the digest is not. openjobs.discover already fails
  # safe on every path it knows about, and openjobs_and_ingest backstops the
  # ones it does not, but a third guard here costs nothing and means no future
  # edit inside that module can take the morning digest down.
  # Set OPENJOBS_ENABLED=0 to switch the wide net off without deleting it.
  OPENJOBS_ENABLED="${OPENJOBS_ENABLED:-1}"
  OPENJOBS_TOP="${OPENJOBS_TOP:-15}"
  OPENJOBS_TIMEOUT="${OPENJOBS_TIMEOUT:-10m}"
  if [ "$OPENJOBS_ENABLED" = "1" ]; then
    echo "--- wide net (open-jobs corpus, top $OPENJOBS_TOP) ---"
    # Capped on wall clock, like the prune sweep. `set -e` isolation only
    # protects against a non-zero exit; it does nothing about a HANG, and
    # requests' 60s timeout is per-read, not total, so a slow-trickling worker
    # serving 14 requests (manifest, centroids, 12 group files) can stall this
    # stage indefinitely without ever tripping it. The digest would then sit
    # behind it and the next cron run would overlap this one.
    openjobs_rc=0
    if command -v timeout >/dev/null 2>&1; then
      timeout "$OPENJOBS_TIMEOUT" "$PY" job_cli.py openjobs --top "$OPENJOBS_TOP" || openjobs_rc=$?
    else
      "$PY" job_cli.py openjobs --top "$OPENJOBS_TOP" || openjobs_rc=$?
    fi
    if [ "$openjobs_rc" -ne 0 ]; then
      echo "WIDE NET FAILED (exit $openjobs_rc, or hit the ${OPENJOBS_TIMEOUT} cap); continuing to the digest"
    fi
  fi
  # Between scan and digest: anything scan just ingested that is already dead
  # gets caught in the same run, and refine does not spend LLM verdict budget
  # ranking postings that no longer exist.
  if [ "$PRUNE_ENABLED" = "1" ] && [ "$(date +%u)" = "$PRUNE_DAY" ]; then
    echo "--- weekly liveness sweep (day $PRUNE_DAY) ---"
    # Isolated on purpose. Every other step here runs bare under `set -e`, so a
    # failure aborts the run; that is right for scan and refine and wrong for
    # this one. The sweep is a nice-to-have and the digest is not, so a failed,
    # hung, or timed-out sweep is logged and stepped over. prune's own summary
    # (N checked: X open, Y closed, Z unknown / M marked skipped) prints to
    # stdout, which this block already captures into the log.
    # The wall-clock cap bounds how late the digest can land, but it is only
    # belt-and-braces: every request liveness.py makes already carries its own
    # timeout. `timeout` is GNU coreutils, so it is there on the Linux host and
    # missing on a bare macOS checkout; when it is missing, run the sweep
    # uncapped rather than not at all.
    prune_rc=0
    if command -v timeout >/dev/null 2>&1; then
      timeout "$PRUNE_TIMEOUT" "$PY" job_cli.py prune --apply || prune_rc=$?
    else
      "$PY" job_cli.py prune --apply || prune_rc=$?
    fi
    if [ "$prune_rc" -ne 0 ]; then
      echo "liveness sweep exited $prune_rc (failed, or hit the ${PRUNE_TIMEOUT} cap); continuing"
    fi
  fi
  # The unattended digest uses deterministic scoring only. Manual refine runs
  # can still request cheap Haiku verdicts explicitly when they add value.
  "$PY" job_cli.py refine --no-llm --top 0 --digest
  echo "=== done ==="
} >>"$LOG" 2>&1
