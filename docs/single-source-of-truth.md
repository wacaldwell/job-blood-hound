# One database

There is exactly **one** `jobs.db`, and it lives on the always-on host:

```
$JOB_HOST:~/job-hound/jobs.db
```

Everything reads and writes that one file: the daily scan and digest
(`bin/daily.sh`), the write API, the MCP server, and you.

## How to use it from a workstation

```
bin/jh list --state applied     # any job_cli command, run against the host
bin/jh draft <ident>            # package is generated ON the host
bin/jh-pull                     # bring the generated packages down locally
bin/jh apply <ident>
```

`bin/jh` is a thin ssh wrapper around `job_cli.py` on the host, targeting
`$JOB_HOST`. `bin/jh-pull` rsyncs `~/job-applications` down so you can attach
the files by hand.

## Do not run job_cli.py directly on a second machine

`job_cli.py` used to fall back to a per-user path
(`~/Library/Application Support/job-monitor/jobs.db` on macOS) and create it on
first open, silently **making a second database**. That is not a cosmetic
problem. It is the bug this design replaced, and the fallback did it again in
August 2026, swallowing nine days of decisions.

Since 2026-08-27 there is no fallback. With `JOB_DB` unset and no `jobs.db` in
the working directory, `jobdb.resolve_db_path` raises and the CLI exits telling
you to use `bin/jh`. It cannot invent a database any more. It can still be
pointed at the wrong one, so the rule stands: drive the host.

## The bug we removed (2026-07-11)

Before that date there were two databases: a workstation working copy and the
host. A scheduled agent ran a sync script four times a day, merging workstation
into host, union by uid, **newer `updated_at` wins**.

That merge rule was unsound, because `updated_at` is not a measure of lifecycle
authority:

- `refine` scores every job through `db.set_fields(...)`, and `set_fields` bumps
  `updated_at` (jobdb.py). The host runs `refine` every morning at 06:30. The
  scan's posting-date and location writes do the same thing.
- So the host rewrites `updated_at` on rows whose **state never changed**.

Concretely: apply to a job on the workstation at 22:05, after the last sync of
the day. Overnight the host's 06:30 scoring pass bumps that row's `updated_at`.
At the 10:00 sync the merge compares timestamps, sees the host as "newer", and
**skips the row**. The apply is silently discarded, and it never recovers: the
workstation's `updated_at` never advances again, so it loses the same
comparison forever.

A scoring event and an "I applied to this job" event are not comparable, but
the merge compared them anyway, and scoring won by being more recent.

Parity was verified before cutover: the host held every workstation row plus
its own, with one intentional difference where the host was correctly ahead.
Nothing was lost. The second database was archived, not deleted.

## Why not just fix the merge

The merge could have been made lifecycle-aware: compare state rank, use
`updated_at` only as a tie-break. That fixes this instance. Collapsing to one
database removes the entire class. There is no second writer, so there is no
conflict to resolve and no merge rule to get wrong.

## Copying the database is not `cp jobs.db`

`jobs.db` runs in WAL mode (`PRAGMA journal_mode = WAL`, set in `jobdb.py`), so
committed rows can be sitting in `jobs.db-wal` while `jobs.db` itself looks
almost empty. A copy of `jobs.db` alone can be an incomplete database.

Use SQLite's own backup, which folds the WAL in:

```bash
sqlite3 ~/job-hound/jobs.db ".backup out.db"
```

If `sqlite3` is not installed, copy `jobs.db*` so the `-wal` and `-shm`
sidecars come along. This applies to backups and to pulling a copy down for
local work.

Whatever you copy down, do not leave it somewhere the CLI might find it. A
stray `jobs.db` in a checkout is the exact bug this document exists to prevent.

## Ad-hoc sqlite3 queries need a timeout

Everything in Python opens the database with `PRAGMA busy_timeout = 5000` and
waits its turn. The `sqlite3` command line tool does not: it defaults to a
timeout of 0 and gives up the instant another process holds the write lock. If
you run the ingest timer every five minutes, a bare
`sqlite3 jobs.db "SELECT ..."` fails with `database is locked` at roughly that
cadence. Observed live on 2026-07-25, two queries out of four during a timer
tick.

Pass a timeout:

```bash
sqlite3 -cmd ".timeout 5000" ~/job-hound/jobs.db "SELECT ..."
```

`bin/jh` and anything going through `JobDB` are unaffected.
