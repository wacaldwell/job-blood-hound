# job-hound write API

`jobapi.py` serves the lead inbox's writes on `127.0.0.1:8765`. It exists so a
local dashboard UI can change job state without opening `jobs.db` itself. It is
the only path by which a UI reaches the database.

Every endpoint is a thin wrapper over an audited `jobdb.py` setter, so `jobdb`
stays the only writer and the state machine lives in one language.

## Environment

Add to the host env file (`~/.job-hound/job-hound.env`, mode 0600, the same one
the systemd units load with `EnvironmentFile=`):

    JOB_API_TOKEN=<a long random string>
    JOB_API_PORT=8765           # optional, this is the default

`JOB_DB` is already set there for the ingest timer and is reused as is.

The same `JOB_API_TOKEN` value goes into the UI's environment, along with
`JOB_API_URL=http://127.0.0.1:8765`.

## Install

`fastapi` and `uvicorn` are dependencies. Install them before the unit starts,
or it crash-loops on `No module named uvicorn` and the only sign is a
connection error.

    .venv/bin/pip install -r requirements.txt
    cp deploy/job-api.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now job-api.service
    systemctl --user status job-api.service

## Verify

    curl -s -o /dev/null -w '%{http_code}\n' \
      -X POST localhost:8765/jobs/nope/read \
      -H "Authorization: Bearer $JOB_API_TOKEN" \
      -H 'Content-Type: application/json' -d '{"read":true}'

404 is correct: the service is up, authenticated, and the job does not exist.
401 means the token does not match. A connection error means it is not running.

## Logs

    journalctl --user -u job-api -f

A connection error on Verify with nothing obviously wrong could be a crash
loop, a startup exception, or a port bind failure. The log is how to tell
which.

## Querying the database by hand

The service runs the database in WAL with `busy_timeout = 5000`, and so does
every other Python caller. The `sqlite3` command line tool does not: it
defaults to a timeout of 0 and fails immediately when another process holds the
write lock. The ingest timer takes that lock every five minutes, so a bare
query fails with `database is locked` at about that rate. Pass a timeout:

    sqlite3 -cmd ".timeout 5000" ~/job-hound/jobs.db "SELECT COUNT(*) FROM jobs;"

This is a property of the CLI tool, not a problem with the service. If you see
`database is locked` from the API itself, that is different and worth the log.

## First deploy against an existing database

The lead-inbox release adds the `read_at` column, its one-way backfill, and WAL
journalling on `jobs.db`. If you are enabling this service on a database that
already holds rows, run these in order.

**Copying jobs.db is not a plain `cp`.** With WAL enabled, committed rows can
be sitting in `jobs.db-wal` while `jobs.db` itself is nearly empty, so copying
that one file can hand you an incomplete database. Use SQLite's own backup,
which folds the WAL in:

    sqlite3 ~/job-hound/jobs.db ".backup out.db"

If `sqlite3` is not installed on the host, copy `jobs.db*` (the glob picks up
the `-wal` and `-shm` sidecars) rather than `jobs.db` alone. This applies to
every copy from here on, including the ones below.

0. **Rehearse the migration on a copy of the real database.** The unit tests
   run against a synthetic pre-inbox schema. This is the one check against the
   actual rows, and it is cheap.

   ```bash
   ssh "$JOB_HOST" 'sqlite3 ~/job-hound/jobs.db ".backup /tmp/jobs-rehearsal.db"'
   scp "$JOB_HOST":/tmp/jobs-rehearsal.db /tmp/jobs-rehearsal.db
   ssh "$JOB_HOST" 'rm -f /tmp/jobs-rehearsal.db'
   ./.venv/bin/python - <<'PY'
   import jobdb
   db = jobdb.JobDB("/tmp/jobs-rehearsal.db")
   total = db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
   unread = db.conn.execute(
       "SELECT COUNT(*) FROM jobs WHERE read_at IS NULL").fetchone()[0]
   print(f"{total} rows, {unread} unread")
   db.close()
   PY
   rm -f /tmp/jobs-rehearsal.db*
   ```

   Expected: every row read, so `unread` is 0. A non-zero count means the
   backfill did not cover the existing rows. Delete the copy afterwards, and
   note the `*` in the `rm`: opening it with this code puts it in WAL, so it
   now has sidecar files too. A stray jobs.db on a second machine is the exact
   bug docs/single-source-of-truth.md exists to prevent.

1. **Back the database up first.** The read-at backfill is one-way, and that
   file is the only copy of the real pipeline.

   ```bash
   ssh "$JOB_HOST" 'sqlite3 ~/job-hound/jobs.db ".backup ~/job-hound/jobs.db.pre-inbox"'
   ```

2. On the host: `git merge --ff-only origin/main`, then
   `.venv/bin/pip install -r requirements.txt`. The pull alone is not enough;
   the API needs the new dependencies.
3. Add `JOB_API_TOKEN` to the host env file (see Environment above).
4. Run the systemd steps under Install to start `job-api.service`, then run
   Verify.
5. **Confirm the UI still reads the database.** This is the one real deployment
   risk: a read-only SQLite connection to a WAL database needs access to the
   `-shm` file. If both processes run as the same user it should be fine, but
   load the jobs view before calling this done.
6. `bin/jh list --state queued` from your workstation, to confirm the CLI still
   works against a WAL database.

## Deploy order

The API must be live before the UI build that calls it, or its writes break in
the gap. Deploy job-hound, start this service, then deploy the UI.

## Hard rule

No endpoint here submits an application, fills an external form, or logs into a
job site. Stamping `applied` is a state write only. Do not add one.
