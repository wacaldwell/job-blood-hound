# Deploying to an always-on host

job-hound is a small Python CLI plus two long-lived services. Nothing here
needs a container runtime or a cloud account. A single always-on Linux box you
can reach over ssh is enough: a home server, a VPS, a Raspberry Pi.

This document is the deployment runbook. Everything below assumes:

- `$JOB_HOST` is your ssh target for that machine (for example
  `deploy@example-host`). `bin/jh` reads the same variable.
- The checkout lives at `~/job-hound` on the host.
- systemd user units are available (`systemctl --user`). If they are not, the
  README files in `deploy/` give cron equivalents.

## Layout on the host

| What | Path |
|------|------|
| Repo | `~/job-hound` |
| Database (the single system of record) | `~/job-hound/jobs.db` |
| Generated packages | `~/job-applications` |
| Secrets and env | `~/.job-hound/job-hound.env` (mode 0600) |
| Logs | `~/logs/job-hound/` |
| Daily cron | `~/job-hound/bin/daily.sh` |

Everything reads and writes that one `jobs.db`: the daily scan, the digest,
the write API, and the MCP server. Do not treat a copy on a laptop as
production. See `docs/single-source-of-truth.md` for why that rule exists.

## One-time setup

```bash
ssh "$JOB_HOST"
git clone <your fork or clone URL> ~/job-hound
cd ~/job-hound
git checkout main
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p ~/job-applications ~/.job-hound
```

Copy the tracked example config files to their un-suffixed names and edit them
(see the README for what each one is for). Then create the env file. Secrets
live there, never in the repo:

```bash
install -m 600 /dev/null ~/.job-hound/job-hound.env
```

Required values:

```bash
ANTHROPIC_API_KEY=...
DISCORD_WEBHOOK_URL=...        # only if you want the daily digest
```

Optional values, defaults shown:

```bash
JOB_DB="$HOME/job-hound/jobs.db"
JOB_APPS_DIR="$HOME/job-applications"
LOG_DIR="$HOME/logs"
JOB_FIT_MODEL="claude-haiku-4-5"
JOB_GATE_MODEL="claude-opus-4-8"
JOB_DRAFT_MODEL="claude-opus-4-8"
```

The component model values are optional because those are the code defaults.
Setting them explicitly on the host makes the cost policy visible beside the
key. `JOB_MODEL` is a global override and should normally stay unset.

Schedule the daily run:

```
30 6 * * * $HOME/job-hound/bin/daily.sh
```

`bin/daily.sh` runs scan, then the open-jobs wide net, then (on one day a week)
the liveness sweep, then a deterministic `refine --no-llm --top 0 --digest`.
That path makes no model calls, so an unattended day costs nothing.

## Code change workflow

The repo uses GitHub Flow:

1. Branch `feature/*`, `fix/*`, or `chore/*` from `main`.
2. Open a PR into `main`.
3. Wait for the `checks` job (`.github/workflows/pr-checks.yml`, which runs
   `pytest -q`) to pass.
4. Merge. Delete the branch locally too, then `git fetch --prune`.
5. Tag `main` for an intentional release when appropriate.

## How a merge reaches the host

`.github/workflows/deploy.yml` runs on a self-hosted GitHub Actions runner
registered to the repo and logged in as the user that owns `~/job-hound` and
the systemd user units. On a push to `main` it:

1. Fast-forwards the checkout (`git merge --ff-only origin/main`, deliberately
   not `reset --hard`, because that directory sits beside `jobs.db`).
2. Installs dependencies only if `requirements.txt` changed.
3. Compiles the tree and imports the entry points, so a syntax or import error
   fails before any service is stopped.
4. Restarts `job-api.service` and retires stale MCP processes if any `.py` or
   `requirements.txt` changed.
5. Verifies the write API answers and the database opens.

Step 4 is deliberately coarse. Any `.py` change restarts everything rather
than mapping files to the service that owns them: `jobapi` imports `jobdb`,
the MCP server imports `job_cli` which imports six more modules, and a
hand-maintained map of that would drift. A restart costs seconds; a service
silently running stale code costs days.

If you are not using a self-hosted runner, the same thing by hand:

```bash
ssh "$JOB_HOST" 'cd ~/job-hound \
  && git fetch origin main \
  && git merge --ff-only origin/main \
  && .venv/bin/pip install -q -r requirements.txt \
  && systemctl --user restart job-api.service \
  && pkill -f "job_hound[_]mcp" || true'
```

Note the bracket in that `pkill` pattern. A bare `pkill -f job_hound_mcp.py`
also matches the shell running it and kills its own job.

The MCP server is spawned per client connection over stdio, so there is nothing
to restart, only stale processes to retire. They respawn on the next call.

## Verify a deploy

Confirm the checkout is where you think it is:

```bash
ssh "$JOB_HOST" 'cd ~/job-hound && git status --short --branch && git log -1 --oneline'
```

Run the read-only pipeline check (no LLM calls, no Discord posting):

```bash
bin/jh stats
```

Expected: pipeline counts print, and `refine --no-llm --top 0` returns a ranked
digest.

Read the database directly. Pass a timeout, because the `sqlite3` command line
tool defaults to 0 and fails the instant another process holds the write lock:

```bash
ssh "$JOB_HOST" 'sqlite3 -cmd ".timeout 5000" ~/job-hound/jobs.db \
  "select state, count(*) from jobs group by state order by state;"'
```

After the first successful manual gate, draft, or refine, verify usage
telemetry:

```bash
ssh "$JOB_HOST" 'tail -n 1 ~/logs/job-hound/llm-usage.log' \
  | jq '{component,model,input_tokens,output_tokens,estimated_cost_usd}'
```

The event must contain no API key and no prompt text.

## Rollback

1. Revert or fix forward on `main` through a PR.
2. Let the deploy workflow apply it, or run the manual sequence above.
3. Restart any long-lived MCP client if MCP code changed.

Do not rebuild or delete `jobs.db` during a rollback. It is the only copy of
your pipeline, and a schema migration is not a reason to throw it away. If you
genuinely need a restore, back it up first with SQLite's own backup command,
which folds the write-ahead log in:

```bash
ssh "$JOB_HOST" 'sqlite3 ~/job-hound/jobs.db ".backup ~/job-hound/jobs.db.bak"'
```

A plain `cp jobs.db` is not sufficient. The database runs in WAL mode, so
committed rows can be sitting in `jobs.db-wal` while `jobs.db` itself looks
nearly empty.
