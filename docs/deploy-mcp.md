# Exposing job-hound as an MCP server

`job_hound_mcp.py` serves the pipeline over the Model Context Protocol, so any
MCP-capable client (an agent runtime, a chat assistant, an editor) can drive the
job search as structured tool calls: discover, triage, gate, draft, mark ready,
record an application you already submitted by hand, skip, close, and inspect
pipeline state.

The server is a thin adapter over the existing CLI and data layer. Every tool
calls the same stage code the CLI calls and returns a plain dict.

Hard rule: job-hound does not submit applications. There is no submit, fill, or
login tool, and `job_apply` only records that a human already applied. Do not
add one.

## Topology

The server talks stdio, so the client spawns it as a subprocess. Two shapes:

**Same host.** The client runs on the machine that holds `jobs.db`. Point it
straight at the interpreter in the checkout's virtualenv.

**Client on another host.** The client tunnels stdio over ssh and starts the
server on the host that holds the database. This is the important case: there
is exactly one `jobs.db`, and the daily cron, the write API, and every MCP
client must all reach that same file. See `docs/single-source-of-truth.md`.

## Launcher

A small wrapper keeps the ssh options and the environment in one place. Put it
somewhere your client can execute, for example `~/mcp-launchers/job-hound.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
JOB_HOST="${JOB_HOST:-deploy@example-host}"
exec ssh \
  -o BatchMode=yes \
  -o ConnectTimeout=10 \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  "$JOB_HOST" \
  'set -euo pipefail
   ENV_FILE="$HOME/.job-hound/job-hound.env"
   if [ -f "$ENV_FILE" ]; then
     set -a
     . "$ENV_FILE"
     set +a
   fi
   export JOB_DB="${JOB_DB:-$HOME/job-hound/jobs.db}"
   export JOB_APPS_DIR="${JOB_APPS_DIR:-$HOME/job-applications}"
   exec "$HOME/job-hound/.venv/bin/python" "$HOME/job-hound/job_hound_mcp.py"'
```

An ssh command like this runs no login shell profile, so the env file has to be
sourced explicitly. Without it `job_draft` fails with
`ANTHROPIC_API_KEY not set`.

Then register it with your client. Most MCP clients take a command and an
argument list. In JSON that is usually:

```json
{
  "mcpServers": {
    "job-hound": {
      "command": "/home/you/mcp-launchers/job-hound.sh",
      "args": []
    }
  }
}
```

For the same-host case, skip the launcher and set `command` to
`~/job-hound/.venv/bin/python` with `args` of
`["~/job-hound/job_hound_mcp.py"]`, plus `JOB_DB` in the client's environment
block.

## Verify

Validate the launcher before restarting anything:

```bash
bash -n ~/mcp-launchers/job-hound.sh
```

Then ask your client to list the server's tools. Expected: connected, 14 tools
discovered.

Confirm it is reading the database you think it is:

```bash
ssh "$JOB_HOST" 'sqlite3 -cmd ".timeout 5000" ~/job-hound/jobs.db \
  "select state, count(*) from jobs group by state order by state;"'
```

Then call the read-only `job_stats` tool and compare counts.

## Activating changes

Two classes of change, with different consequences.

**Launcher or client config.** Restart or reload the client so the long-lived
subprocess is recreated.

**job-hound code.** Follow the workflow in `docs/deploy-tools-host.md`: branch,
PR into `main`, merge, let the deploy reach the host. A pull only updates files
on disk. It does not replace an already-running MCP process, so retire the
stale one:

```bash
ssh "$JOB_HOST" 'pkill -f "job_hound[_]mcp" || true'
```

Note the bracket in that pattern. A bare `pkill -f job_hound_mcp.py` also
matches the shell running it and kills its own job. The server respawns on the
next client connection.

## Smoke tests

Cheap and read-only first:

1. `job_stats`, pipeline counts
2. `job_list` or `job_refine(no_llm=True)`, ranked leads
3. `job_show <slug>`, one job detail

State-changing calls only when intended:

- `job_queue`
- `job_ready`
- `job_apply`, records a human submission only
- `job_skip`
- `job_close`
- `job_state`, recovery only

Confirm before expensive calls:

- `job_gate`, one model call
- `job_draft`, spends API budget and writes generated package files

## Rollback

- Restore the previous launcher if a launcher change broke it.
- Remove or disable the server entry in the client config and restart the
  client.
- Revert or fix forward job-hound code through a PR. Do not hand-edit the
  deployed checkout except for an emergency rollback.
