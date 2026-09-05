# Lead Inbox (job-hound half) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give job-hound a `read_at` column, two audited setters, and a localhost write API so the lead inbox UI can record votes, notes, and lifecycle transitions without ever writing the database itself.

**Architecture:** `jobdb.py` stays the only writer and the only home of the state machine. A thin FastAPI service (`jobapi.py`) wraps its audited setters and is called by the lead inbox UI over localhost HTTP. The one endpoint that matters most is `GET /jobs/{ident}/transitions`, which lets the UI render only legal actions without holding a copy of `TRANSITIONS`.

**Tech Stack:** Python 3.13, SQLite (WAL), FastAPI, uvicorn, pytest.

**Spec:** `docs/superpowers/specs/2026-07-25-lead-inbox-design.md`

**Branch:** `feat/lead-inbox` (already created, spec already committed as `564cb76`)

## Global Constraints

- **No em dashes, ever.** Applies to code comments, docstrings, docs, and commit messages. Use commas, parentheses, or separate sentences.
- **Commit format:** `[Component]: Brief description of change`.
- **GitHub Flow.** Work on `feat/lead-inbox`. `main` is protected; changes land through a PR with a passing `tests` check. Never push to `main`.
- **Tests are flat `test_*.py` files in the repo root.** There is no `tests/` directory. Follow the existing naming.
- **`conftest.py` isolates `JOB_DB` and `JOB_APPS_DIR` with autouse fixtures.** Do not remove or weaken them. They are the reason the suite cannot create a second `jobs.db`.
- **Never run `python job_cli.py` on the Mac.** There is exactly one real `jobs.db` and it lives on the tools host.
- **Run tests from the venv:** `./.venv/bin/python -m pytest`.
- **Hard rule, non-negotiable:** no endpoint may submit an application, fill an external form, or log into a job site. Stamping `applied` is a state write only.
- **New dependencies are limited to `fastapi`, `uvicorn`, and `httpx`.** Nothing else gets added.

## File Structure

| File | Responsibility |
|---|---|
| `jobdb.py` (modify) | `read_at` column, one-time backfill, `set_read`, `set_notes`, `set_fields` guard, WAL |
| `jobapi.py` (create) | FastAPI app: auth, resolve, five endpoints. No business logic of its own. |
| `bin/job-api.sh` (create) | Launch uvicorn from the venv, mirroring `bin/ingest-queue.sh` |
| `deploy/job-api.service` (create) | systemd user unit, long-running, restart on failure |
| `deploy/README-job-api.md` (create) | Install and env-var runbook |
| `test_read_at_migration.py` (create) | The migration and its one-shot backfill |
| `test_lead_inbox_setters.py` (create) | `set_read`, `set_notes`, and the deliberate fit-corpus coupling |
| `test_jobdb_wal.py` (create) | WAL enabled, concurrent reader not blocked |
| `test_jobapi.py` (create) | Auth, resolution, all five endpoints, 409 semantics |
| `CLAUDE.md` (modify) | Document the new service, env vars, and the read/unread concept |

---

### Task 1: The `read_at` column and its one-shot backfill

This is the task the whole feature rests on. If the backfill can re-run, every lead discovered after the migration gets marked read before the operator sees it and the unread queue is permanently empty.

**Files:**
- Modify: `jobdb.py` (SCHEMA around line 119, `ADDED_COLUMNS` around line 199, `_migrate` around line 240, `set_fields` around line 343)
- Test: `test_read_at_migration.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `jobs.read_at` TEXT (ISO timestamp, NULL means unread) and the module constant `_AUDITED_COLUMNS = {"read_at"}`.

- [ ] **Step 1: Write the failing tests**

Create `test_read_at_migration.py`:

```python
"""The unread queue depends entirely on read_at being NULL for new leads.

The backfill that marks every pre-inbox lead read must fire exactly once, at
the migration that adds the column. A backfill in the body of _migrate would
re-run on every JobDB open (the scan opens it daily, the ingest timer every
five minutes) and mark each newly discovered lead read before the operator ever saw
it. The queue would always be empty and nothing would look broken.
"""
import sqlite3

import pytest

import jobdb

# A jobs table from before the inbox shipped: no read_at column.
PRE_INBOX_SCHEMA = """
CREATE TABLE jobs (
    uid TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL, ext_id TEXT NOT NULL,
    ats TEXT NOT NULL, company TEXT NOT NULL, title TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'discovered',
    discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


def _pre_inbox_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(PRE_INBOX_SCHEMA)
    conn.execute(
        "INSERT INTO jobs (uid, slug, ext_id, ats, company, title, state, "
        "discovered_at, updated_at) VALUES "
        "('greenhouse:acme:9', 'acme__old-role__9', '9', 'greenhouse', "
        "'acme', 'Old Role', 'discovered', '2026-01-01', '2026-01-01')")
    conn.commit()
    conn.close()


def test_migration_adds_the_column_and_marks_pre_existing_leads_read(tmp_path):
    """Start clean: the 411 leads that predate the inbox are not a backlog."""
    path = tmp_path / "old.db"
    _pre_inbox_db(path)

    db = jobdb.JobDB(path)
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(jobs)")}
    assert "read_at" in cols
    assert db.get("greenhouse:acme:9")["read_at"] is not None
    db.close()


def test_the_backfill_never_runs_a_second_time(tmp_path):
    """The test that protects the feature. A lead discovered after the
    migration must survive later opens still unread."""
    path = tmp_path / "old.db"
    _pre_inbox_db(path)

    db = jobdb.JobDB(path)
    db.upsert_job({"id": "1", "ats": "lever", "company": "beta",
                   "title": "Platform Lead", "location": "Remote"})
    new_uid = jobdb.make_job_uid("lever", "beta", "1")
    assert db.get(new_uid)["read_at"] is None
    db.close()

    db = jobdb.JobDB(path)  # second open, the migration path runs again
    assert db.get(new_uid)["read_at"] is None, \
        "the backfill re-ran and marked a new lead read"
    db.close()


def test_a_fresh_database_starts_with_everything_unread(tmp_path):
    """A brand new DB has no pre-existing leads, so nothing to backfill."""
    db = jobdb.JobDB(tmp_path / "new.db")
    db.upsert_job({"id": "1", "ats": "greenhouse", "company": "acme",
                   "title": "Senior SRE", "location": "Remote"})
    uid = jobdb.make_job_uid("greenhouse", "acme", "1")
    assert db.get(uid)["read_at"] is None
    db.close()


def test_set_fields_refuses_to_write_read_at(tmp_path):
    """set_fields writes no state_log row. An unaudited read stamp would
    drain the queue with no record of what did it."""
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"id": "1", "ats": "greenhouse", "company": "acme",
                   "title": "Senior SRE", "location": "Remote"})
    uid = jobdb.make_job_uid("greenhouse", "acme", "1")
    with pytest.raises(ValueError, match="read_at"):
        db.set_fields(uid, read_at="2026-07-25T00:00:00+00:00")
    db.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest test_read_at_migration.py -v`
Expected: FAIL. The migration tests fail on `assert "read_at" in cols`, and the guard test fails because `set_fields` accepts the write.

- [ ] **Step 3: Add the column to SCHEMA**

In `jobdb.py`, in the `jobs` table inside `SCHEMA`, add a line directly after `digested_at`:

```
    read_at       TEXT,                   -- when the operator processed this lead; NULL = unread
```

- [ ] **Step 4: Add it to ADDED_COLUMNS**

In `ADDED_COLUMNS`, add after `"digested_at": "TEXT",`:

```python
    "read_at": "TEXT",
```

- [ ] **Step 5: Backfill inside the add branch**

In `_migrate`, replace the jobs-column loop with:

```python
        for col, decl in ADDED_COLUMNS.items():
            if col not in existing:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
                if col == "read_at":
                    # Start clean. Every lead that existed before the inbox
                    # shipped counts as already processed, so the queue opens
                    # empty and fills from the next scan.
                    #
                    # This runs INSIDE the add branch so it fires exactly once,
                    # at the migration. A "WHERE read_at IS NULL" backfill in
                    # the body of _migrate would look equivalent and would be
                    # catastrophic: it re-runs on every open, so every newly
                    # discovered lead would be stamped read before the operator saw it
                    # and the unread queue would be permanently empty. The
                    # gate_model backfill below is safe to re-run only because
                    # its WHERE clause is self-limiting. This one is not.
                    self.conn.execute(
                        "UPDATE jobs SET read_at = ?", (now_iso(),))
```

- [ ] **Step 6: Add the set_fields guard**

In `jobdb.py`, after the `_GATE_COLUMNS` definition, add:

```python
# read_at has a dedicated audited setter (set_read) for the same reason the
# gate columns do: set_fields writes no state_log row, and an unaudited read
# stamp would silently drain the unread queue.
#
# notes deliberately stays writable by set_fields. It gates nothing, two
# existing tests use it as the example of a legitimate generic write, and
# set_notes is the audited path rather than a prohibition on the generic one.
_AUDITED_COLUMNS = {"read_at"}
```

In `set_fields`, directly after the existing `_GATE_COLUMNS` check, add:

```python
        bad_audited = _AUDITED_COLUMNS & set(fields)
        if bad_audited:
            raise ValueError(
                f"set_fields cannot write {sorted(bad_audited)}. "
                "Use set_read, which is audited.")
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `./.venv/bin/python -m pytest test_read_at_migration.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 8: Run the full suite for regressions**

Run: `./.venv/bin/python -m pytest -q`
Expected: PASS. Pay attention to `test_jobdb_schema.py` and `test_jobdb_set_fields_gate_guard.py`, which exercise the same code paths.

- [ ] **Step 9: Commit**

```bash
git add jobdb.py test_read_at_migration.py
git commit -m "[jobdb]: add read_at with a backfill that can only fire once

The unread queue is defined by read_at IS NULL, so the backfill that marks
pre-inbox leads read runs inside the ALTER TABLE branch rather than the body
of _migrate. In the body it would re-run on every open and stamp every newly
discovered lead read before the operator saw it."
```

---

### Task 2: `set_read` and `set_notes`

**Files:**
- Modify: `jobdb.py` (add both methods after `set_vote`, around line 386)
- Test: `test_lead_inbox_setters.py`

**Interfaces:**
- Consumes: `read_at` from Task 1.
- Produces: `JobDB.set_read(uid, read=True) -> sqlite3.Row`, `JobDB.set_notes(uid, text) -> sqlite3.Row`, and the module constant `NOTE_MAX = 4000`. Both raise `ValueError` on an unknown uid. Both append a `state_log` row with state unchanged.

- [ ] **Step 1: Write the failing tests**

Create `test_lead_inbox_setters.py`:

```python
"""The two audited setters the inbox writes through, plus the one coupling
that is easy to introduce by accident: a note becomes training data.
"""
import pytest

import fit
import jobdb


def _db(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"id": "1", "ats": "greenhouse", "company": "acme",
                   "title": "Senior SRE", "location": "Remote"})
    return db, jobdb.make_job_uid("greenhouse", "acme", "1")


def test_set_read_stamps_and_audits(tmp_path):
    db, uid = _db(tmp_path)
    row = db.set_read(uid)
    assert row["read_at"] is not None
    notes = [h["note"] for h in db.history(uid)]
    assert "read" in notes
    db.close()


def test_set_read_false_returns_it_to_the_queue(tmp_path):
    db, uid = _db(tmp_path)
    db.set_read(uid)
    row = db.set_read(uid, read=False)
    assert row["read_at"] is None
    assert "unread" in [h["note"] for h in db.history(uid)]
    db.close()


def test_set_read_does_not_change_state(tmp_path):
    db, uid = _db(tmp_path)
    db.set_read(uid)
    assert db.get(uid)["state"] == "discovered"
    db.close()


def test_set_notes_writes_and_audits(tmp_path):
    db, uid = _db(tmp_path)
    row = db.set_notes(uid, "Recruiter reached out directly.")
    assert row["notes"] == "Recruiter reached out directly."
    assert any(h["note"].startswith("note: Recruiter")
               for h in db.history(uid))
    db.close()


def test_empty_note_clears_the_column(tmp_path):
    db, uid = _db(tmp_path)
    db.set_notes(uid, "something")
    row = db.set_notes(uid, "   ")
    assert row["notes"] is None
    assert "note: cleared" in [h["note"] for h in db.history(uid)]
    db.close()


def test_a_long_note_is_capped_not_rejected(tmp_path):
    db, uid = _db(tmp_path)
    row = db.set_notes(uid, "x" * (jobdb.NOTE_MAX + 500))
    assert len(row["notes"]) == jobdb.NOTE_MAX
    db.close()


def test_both_setters_reject_an_unknown_uid(tmp_path):
    db, _ = _db(tmp_path)
    with pytest.raises(ValueError):
        db.set_read("greenhouse:nope:1")
    with pytest.raises(ValueError):
        db.set_notes("greenhouse:nope:1", "hi")
    db.close()


def test_a_note_becomes_the_pursued_reason_in_the_fit_corpus(tmp_path):
    """Deliberate coupling, asserted so it stays a decision.

    fit.build_history reads notes as the stated reason for any job in a
    pursued state, so an inbox note becomes part of the few-shot corpus that
    teaches the ranker. See the design spec, section 1.
    """
    db, uid = _db(tmp_path)
    db.set_notes(uid, "AWS partnership scope, exactly the work I want.")
    db.set_state(uid, "queued")

    entry = [h for h in fit.build_history(db) if h["company"] == "acme"][0]
    assert entry["decision"] == "pursued"
    assert entry["reason"] == "AWS partnership scope, exactly the work I want."
    db.close()


def test_a_note_on_an_untriaged_lead_stays_out_of_the_corpus(tmp_path):
    """A discovered lead enters the corpus only via a vote, and that branch
    reads vote_note. A triage note on a lead never pursued teaches nothing."""
    db, uid = _db(tmp_path)
    db.set_notes(uid, "not sure about this one")
    assert [h for h in fit.build_history(db) if h["company"] == "acme"] == []
    db.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest test_lead_inbox_setters.py -v`
Expected: FAIL with `AttributeError: 'JobDB' object has no attribute 'set_read'`.

- [ ] **Step 3: Add the NOTE_MAX constant**

In `jobdb.py`, directly after the `_AUDITED_COLUMNS` block from Task 1:

```python
# A working note about a lead, not a one-line vote reason. vote_note is capped
# at 280 by the API that writes it; this is the field you paste a recruiter
# email into.
NOTE_MAX = 4000
```

- [ ] **Step 4: Implement both setters**

In `jobdb.py`, directly after `set_vote` (which ends around line 386), add:

```python
    def set_read(self, uid, read=True):
        """Mark a lead processed, or push it back into the unread queue.

        read_at IS NULL is the only definition of unread. Audited in state_log
        with the state unchanged, the same shape as set_vote, so `jh show`
        history shows when a lead left the queue and when it came back.
        """
        row = self.get(uid)
        if not row:
            raise ValueError(f"no job with uid {uid}")
        ts = now_iso()
        self.conn.execute(
            "UPDATE jobs SET read_at = ?, updated_at = ? WHERE uid = ?",
            (ts if read else None, ts, uid))
        self.conn.execute(
            "INSERT INTO state_log (job_uid, from_state, to_state, at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, row["state"], row["state"], ts, "read" if read else "unread"))
        self.conn.commit()
        return self.get(uid)

    def set_notes(self, uid, text):
        """Freeform working note on a lead. Audited, and load-bearing.

        fit.build_history reads notes as the stated reason for any job in a
        pursued state, so what lands here becomes part of the corpus that
        teaches the ranker. That is deliberate (see the lead-inbox design
        spec), which is why it goes through an audited setter rather than
        set_fields. Empty or whitespace-only text clears the column.
        """
        row = self.get(uid)
        if not row:
            raise ValueError(f"no job with uid {uid}")
        clean = (text or "").strip()[:NOTE_MAX] or None
        ts = now_iso()
        self.conn.execute(
            "UPDATE jobs SET notes = ?, updated_at = ? WHERE uid = ?",
            (clean, ts, uid))
        summary = clean.splitlines()[0] if clean else "cleared"
        self.conn.execute(
            "INSERT INTO state_log (job_uid, from_state, to_state, at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, row["state"], row["state"], ts, f"note: {summary}"))
        self.conn.commit()
        return self.get(uid)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/bin/python -m pytest test_lead_inbox_setters.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 6: Run the full suite**

Run: `./.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add jobdb.py test_lead_inbox_setters.py
git commit -m "[jobdb]: audited set_read and set_notes for the lead inbox

set_notes writes the notes column, which fit.build_history already reads as
the pursued-reason in the ranker's few-shot corpus. That coupling is wanted
and is now asserted by a test so it stays a decision rather than an accident."
```

---

### Task 3: WAL and busy_timeout

**Files:**
- Modify: `jobdb.py` (`__init__`, around line 228)
- Test: `test_jobdb_wal.py`

**Interfaces:**
- Consumes: nothing.
- Produces: every `JobDB` connection runs in WAL with a 5 second busy timeout.

- [ ] **Step 1: Write the failing tests**

Create `test_jobdb_wal.py`:

```python
"""Four processes open this database on the host: the write API, the nightly
scan, the 5-minute ingest timer, and bin/jh. the lead inbox UI opens it a fifth
time, read-only. In the default rollback journal a writer blocks every
reader, which with a synchronous API in the mix means the UI hangs behind the
scan. WAL is what makes concurrent access boring.
"""
import jobdb


def _job(n):
    return {"id": str(n), "ats": "greenhouse", "company": "acme",
            "title": "Senior SRE", "location": "Remote"}


def test_wal_is_enabled(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    db.close()


def test_busy_timeout_is_set(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    assert db.conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    db.close()


def test_a_reader_is_not_blocked_by_an_open_write(tmp_path):
    """The reason WAL is here. Under the old journal mode this raises
    'database is locked'."""
    path = tmp_path / "t.db"
    writer = jobdb.JobDB(path)
    writer.upsert_job(_job(1))
    uid = jobdb.make_job_uid("greenhouse", "acme", "1")

    reader = jobdb.JobDB(path)
    writer.conn.execute("BEGIN IMMEDIATE")
    writer.conn.execute(
        "UPDATE jobs SET title = 'changed' WHERE uid = ?", (uid,))

    assert reader.get(uid)["title"] == "Senior SRE"

    writer.conn.rollback()
    reader.close()
    writer.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest test_jobdb_wal.py -v`
Expected: FAIL. `journal_mode` returns `delete`, and the concurrent read raises `sqlite3.OperationalError: database is locked`.

- [ ] **Step 3: Enable WAL in the constructor**

In `jobdb.py`, replace the connect lines in `__init__`:

```python
        self.conn = sqlite3.connect(str(self.path), timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        # The write API, the nightly scan, the ingest timer and bin/jh all
        # open this file, and the lead inbox UI opens it read-only alongside
        # them. WAL lets readers run while a writer holds the lock; the busy
        # timeout makes a second writer wait its turn instead of failing
        # immediately with "database is locked".
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/bin/python -m pytest test_jobdb_wal.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 5: Run the full suite**

Run: `./.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add jobdb.py test_jobdb_wal.py
git commit -m "[jobdb]: run in WAL with a busy timeout

A synchronous write API alongside the nightly scan and the ingest timer means
concurrent access stops being theoretical. WAL keeps readers unblocked."
```

---

### Task 4: The write API, auth and the three simple endpoints

**Files:**
- Create: `jobapi.py`
- Modify: `requirements.txt`
- Test: `test_jobapi.py`

**Interfaces:**
- Consumes: `set_vote` (existing), `set_read` and `set_notes` from Task 2.
- Produces: `jobapi.app` (a `FastAPI` instance), `jobapi._payload(row) -> dict` with keys `uid`, `slug`, `state`, `vote`, `vote_note`, `notes`, `read_at`, `updated_at`. Endpoints `POST /jobs/{ident}/vote`, `POST /jobs/{ident}/note`, `POST /jobs/{ident}/read`. Task 5 adds two more endpoints to this same module.

- [ ] **Step 1: Add the dependencies**

Append to `requirements.txt`:

```
fastapi
uvicorn
httpx
```

Then install: `./.venv/bin/python -m pip install -r requirements.txt`

(`httpx` is required by FastAPI's `TestClient`, so it is a test dependency of this repo's only dependency file.)

- [ ] **Step 2: Write the failing tests**

Create `test_jobapi.py`:

```python
"""The write API is the only way the lead inbox UI reaches the database. Every
endpoint is a thin wrapper over an audited jobdb setter, so these tests cover
the wrapper: auth, identifier resolution, and status codes.
"""
import pytest
from fastapi.testclient import TestClient

import jobapi
import jobdb

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    db = jobdb.JobDB(path)
    db.upsert_job({"id": "1", "ats": "greenhouse", "company": "acme",
                   "title": "Senior SRE", "location": "Remote"})
    db.close()
    monkeypatch.setenv("JOB_DB", str(path))
    monkeypatch.setenv("JOB_API_TOKEN", "test-token")
    return TestClient(jobapi.app)


@pytest.fixture
def slug():
    return jobdb.make_slug(
        "acme", "Senior SRE", jobdb.make_job_uid("greenhouse", "acme", "1"))


def test_a_request_without_a_token_is_rejected(client, slug):
    r = client.post(f"/jobs/{slug}/read", json={"read": True})
    assert r.status_code == 401


def test_a_request_with_the_wrong_token_is_rejected(client, slug):
    r = client.post(f"/jobs/{slug}/read", json={"read": True},
                    headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_an_unknown_identifier_is_404(client):
    r = client.post("/jobs/nosuchjob/read", json={"read": True}, headers=AUTH)
    assert r.status_code == 404


def test_read_marks_the_lead_processed(client, slug):
    r = client.post(f"/jobs/{slug}/read", json={"read": True}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["read_at"] is not None


def test_read_false_returns_it_to_the_queue(client, slug):
    client.post(f"/jobs/{slug}/read", json={"read": True}, headers=AUTH)
    r = client.post(f"/jobs/{slug}/read", json={"read": False}, headers=AUTH)
    assert r.json()["read_at"] is None


def test_vote_records_a_vote_and_its_note(client, slug):
    r = client.post(f"/jobs/{slug}/vote",
                    json={"vote": "up", "note": "great scope"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["vote"] == "up"
    assert r.json()["vote_note"] == "great scope"


def test_clearing_a_vote_is_allowed(client, slug):
    client.post(f"/jobs/{slug}/vote", json={"vote": "up"}, headers=AUTH)
    r = client.post(f"/jobs/{slug}/vote", json={"vote": None}, headers=AUTH)
    assert r.json()["vote"] is None


def test_an_invalid_vote_is_400(client, slug):
    r = client.post(f"/jobs/{slug}/vote", json={"vote": "sideways"},
                    headers=AUTH)
    assert r.status_code == 400


def test_note_writes_the_notes_column(client, slug):
    r = client.post(f"/jobs/{slug}/note",
                    json={"text": "Recruiter emailed directly."}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["notes"] == "Recruiter emailed directly."


def test_a_unique_slug_prefix_resolves(client, slug):
    r = client.post(f"/jobs/{slug[:12]}/read", json={"read": True},
                    headers=AUTH)
    assert r.status_code == 200
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest test_jobapi.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobapi'`.

- [ ] **Step 4: Write jobapi.py**

Create `jobapi.py`:

```python
#!/usr/bin/env python3
"""
jobapi.py - the local write API for the job pipeline.

the lead inbox UI opens jobs.db read-only and cannot write it. This service is
how the lead inbox records the three things a human does while triaging: a
vote, a note, and a lifecycle transition. Every endpoint is a thin wrapper
around an audited jobdb.py setter, so jobdb.py stays the only writer and the
state machine lives in exactly one language.

Bound to localhost, bearer token from JOB_API_TOKEN. Not exposed off the host.

HARD RULE: nothing here submits an application, fills an external form, or
logs into a job site. Stamping 'applied' is a state write only, the same thing
the MCP server's job_apply already does. Do not add an endpoint that crosses
that line.
"""
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

import jobdb


def _db():
    path = os.environ.get("JOB_DB")
    if not path:
        raise HTTPException(503, "JOB_DB is not configured")
    db = jobdb.JobDB(path)
    try:
        yield db
    finally:
        db.close()


def _auth(authorization: str = Header(default="")):
    token = os.environ.get("JOB_API_TOKEN", "")
    if not token:
        raise HTTPException(503, "JOB_API_TOKEN is not configured")
    if authorization != f"Bearer {token}":
        raise HTTPException(401, "bad or missing bearer token")


app = FastAPI(title="job-hound write API", dependencies=[Depends(_auth)])


def _resolve(db, ident):
    """Accept a uid, a slug, or a unique slug prefix, exactly like bin/jh."""
    try:
        row = db.resolve(ident)
    except jobdb.TransitionError as e:   # an ambiguous prefix
        raise HTTPException(400, str(e))
    if not row:
        raise HTTPException(404, f"no job matching '{ident}'")
    return row


def _payload(row):
    """The fields the inbox re-renders after a write. Not the whole row."""
    return {
        "uid": row["uid"],
        "slug": row["slug"],
        "state": row["state"],
        "vote": row["vote"],
        "vote_note": row["vote_note"],
        "notes": row["notes"],
        "read_at": row["read_at"],
        "updated_at": row["updated_at"],
    }


class VoteIn(BaseModel):
    vote: str | None = None
    note: str | None = None


@app.post("/jobs/{ident}/vote")
def post_vote(ident: str, body: VoteIn, db=Depends(_db)):
    row = _resolve(db, ident)
    try:
        return _payload(db.set_vote(row["uid"], body.vote, note=body.note))
    except ValueError as e:
        raise HTTPException(400, str(e))


class NoteIn(BaseModel):
    text: str = ""


@app.post("/jobs/{ident}/note")
def post_note(ident: str, body: NoteIn, db=Depends(_db)):
    row = _resolve(db, ident)
    return _payload(db.set_notes(row["uid"], body.text))


class ReadIn(BaseModel):
    read: bool = True


@app.post("/jobs/{ident}/read")
def post_read(ident: str, body: ReadIn, db=Depends(_db)):
    row = _resolve(db, ident)
    return _payload(db.set_read(row["uid"], read=body.read))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/bin/python -m pytest test_jobapi.py -v`
Expected: PASS, 10 tests.

- [ ] **Step 6: Commit**

```bash
git add jobapi.py test_jobapi.py requirements.txt
git commit -m "[jobapi]: localhost write API for votes, notes, and read state

the lead inbox UI keeps its read-only handle on jobs.db. Every write goes
through an audited jobdb setter behind a bearer token on 127.0.0.1."
```

---

### Task 5: State transitions and the legal-moves endpoint

`GET /jobs/{ident}/transitions` is the reason this is an API and not a spool. It lets the UI show only legal actions while `TRANSITIONS` stays in `jobdb.py` alone.

**Files:**
- Modify: `jobapi.py` (append to the end)
- Test: `test_jobapi_state.py`

**Interfaces:**
- Consumes: `jobapi.app`, `jobapi._resolve`, `jobapi._payload` from Task 4; `jobdb.set_state`, `jobdb.STATES`, `jobdb.TRANSITIONS`, `jobdb.TransitionError`.
- Produces: `POST /jobs/{ident}/state` and `GET /jobs/{ident}/transitions`, which returns `{"state": str, "next": list[str]}`.

- [ ] **Step 1: Write the failing tests**

Create `test_jobapi_state.py`:

```python
"""Advancing state is the action that made this an API instead of a spool: it
needs a synchronous yes or no. An illegal jump has to come back as an error
the UI can show, not land silently and surface later as a broken row.
"""
import pytest
from fastapi.testclient import TestClient

import jobapi
import jobdb

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    db = jobdb.JobDB(path)
    db.upsert_job({"id": "1", "ats": "greenhouse", "company": "acme",
                   "title": "Senior SRE", "location": "Remote"})
    db.close()
    monkeypatch.setenv("JOB_DB", str(path))
    monkeypatch.setenv("JOB_API_TOKEN", "test-token")
    return TestClient(jobapi.app)


@pytest.fixture
def slug():
    return jobdb.make_slug(
        "acme", "Senior SRE", jobdb.make_job_uid("greenhouse", "acme", "1"))


def test_a_legal_transition_succeeds(client, slug):
    r = client.post(f"/jobs/{slug}/state", json={"state": "queued"},
                    headers=AUTH)
    assert r.status_code == 200
    assert r.json()["state"] == "queued"


def test_advancing_state_also_marks_the_lead_read(client, slug):
    """Queueing a lead is an explicit disposition. Leaving it in the unread
    queue afterwards would be a bug."""
    r = client.post(f"/jobs/{slug}/state", json={"state": "queued"},
                    headers=AUTH)
    assert r.json()["read_at"] is not None


def test_an_illegal_transition_is_409_with_the_reason(client, slug):
    r = client.post(f"/jobs/{slug}/state", json={"state": "applied"},
                    headers=AUTH)
    assert r.status_code == 409
    assert "illegal transition" in r.json()["detail"]
    assert "discovered -> applied" in r.json()["detail"]


def test_an_illegal_transition_changes_nothing(client, slug):
    client.post(f"/jobs/{slug}/state", json={"state": "applied"}, headers=AUTH)
    r = client.get(f"/jobs/{slug}/transitions", headers=AUTH)
    assert r.json()["state"] == "discovered"


def test_an_unknown_state_is_400(client, slug):
    r = client.post(f"/jobs/{slug}/state", json={"state": "banana"},
                    headers=AUTH)
    assert r.status_code == 400


def test_transitions_lists_only_legal_next_states(client, slug):
    r = client.get(f"/jobs/{slug}/transitions", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"state": "discovered", "next": ["queued", "skipped"]}


def test_transitions_follows_the_lead_as_it_moves(client, slug):
    client.post(f"/jobs/{slug}/state", json={"state": "queued"}, headers=AUTH)
    r = client.get(f"/jobs/{slug}/transitions", headers=AUTH)
    assert r.json()["state"] == "queued"
    assert r.json()["next"] == ["discovered", "drafted", "skipped"]


def test_a_terminal_state_offers_nothing(client, slug):
    for s in ("queued", "drafted", "ready", "applied", "closed"):
        client.post(f"/jobs/{slug}/state", json={"state": s}, headers=AUTH)
    r = client.get(f"/jobs/{slug}/transitions", headers=AUTH)
    assert r.json() == {"state": "closed", "next": []}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest test_jobapi_state.py -v`
Expected: FAIL with 404 responses, because neither route exists yet.

- [ ] **Step 3: Add both endpoints**

Append to `jobapi.py`:

```python
class StateIn(BaseModel):
    state: str
    note: str | None = None
    outcome: str | None = None


@app.post("/jobs/{ident}/state")
def post_state(ident: str, body: StateIn, db=Depends(_db)):
    """Advance a lead through the lifecycle.

    An unknown state is a bad request; a known state that is not reachable
    from here is a conflict, and the message from TransitionError travels to
    the UI verbatim so it can say exactly what is not allowed.
    """
    if body.state not in jobdb.STATES:
        raise HTTPException(400, f"unknown state: {body.state}")
    row = _resolve(db, ident)
    try:
        updated = db.set_state(row["uid"], body.state, note=body.note,
                               outcome=body.outcome)
    except jobdb.TransitionError as e:
        raise HTTPException(409, str(e))
    # Advancing state is an explicit disposition, so the lead leaves the
    # unread queue in the same call. Requiring a separate keypress afterwards
    # would leave queued leads sitting in the inbox, which is the bug this
    # whole surface exists to fix.
    return _payload(db.set_read(updated["uid"], read=True))


@app.get("/jobs/{ident}/transitions")
def get_transitions(ident: str, db=Depends(_db)):
    """The legal next states for this lead.

    This endpoint is why the inbox is an API client and not a spool writer:
    the UI renders only legal actions without ever holding a copy of
    TRANSITIONS. The state machine stays in one file, in one language.
    """
    row = _resolve(db, ident)
    return {"state": row["state"],
            "next": sorted(jobdb.TRANSITIONS.get(row["state"], set()))}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/bin/python -m pytest test_jobapi_state.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Run the full suite**

Run: `./.venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add jobapi.py test_jobapi_state.py
git commit -m "[jobapi]: state transitions with legal-moves discovery

An illegal jump returns 409 carrying the TransitionError message, and
GET /transitions lets the UI show only legal actions without duplicating
TRANSITIONS in TypeScript."
```

---

### Task 6: Service wiring and documentation

**Files:**
- Create: `bin/job-api.sh`, `deploy/job-api.service`, `deploy/README-job-api.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: `jobapi.app` from Tasks 4 and 5.
- Produces: a systemd user unit `job-api.service` serving on `127.0.0.1:${JOB_API_PORT:-8765}`.

- [ ] **Step 1: Create the launcher**

Create `bin/job-api.sh`:

```bash
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
```

- [ ] **Step 2: Make it executable and smoke test it**

```bash
chmod +x bin/job-api.sh
JOB_DB=/tmp/smoke-jobs.db JOB_API_TOKEN=smoke ./bin/job-api.sh &
sleep 2
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST localhost:8765/jobs/nope/read \
  -H 'Authorization: Bearer smoke' -H 'Content-Type: application/json' \
  -d '{"read":true}'
kill %1
rm -f /tmp/smoke-jobs.db
```

Expected: `404`. A 401 means the token is not reaching the process; a connection error means uvicorn did not start.

- [ ] **Step 3: Create the systemd unit**

Create `deploy/job-api.service`:

```ini
[Unit]
Description=job-hound write API (the lead inbox UI lead inbox)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/job-hound
# Reuse job-hound's env file. JOB_API_TOKEN must be added to it; the same
# value goes into lead-inbox's environment as JOB_API_TOKEN.
EnvironmentFile=%h/.lead-inbox/lead-inbox.env
ExecStart=%h/job-hound/bin/job-api.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

- [ ] **Step 4: Write the deploy runbook**

Create `deploy/README-job-api.md`:

```markdown
# job-hound write API

Serves the lead inbox's writes on `127.0.0.1:8765`. the lead inbox UI calls it;
nothing else should. It is the only path by which the UI reaches jobs.db.

## Environment

Add to `~/.config/job-hound/job-hound.env` (the shared env file both services read):

    JOB_API_TOKEN=<a long random string>
    JOB_API_PORT=8765           # optional, this is the default

`JOB_DB` is already set there for the ingest timer and is reused as is.

The same `JOB_API_TOKEN` value goes into the lead inbox UI's environment, along
with `JOB_API_URL=http://127.0.0.1:8765`.

## Install

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

## Deploy order

The API must be live before the lead inbox UI's build that calls it, or voting
breaks in the gap. Deploy job-hound, start this service, then deploy lead-inbox.

## Hard rule

No endpoint here submits an application, fills an external form, or logs into
a job site. Stamping `applied` is a state write only. Do not add one.
```

- [ ] **Step 5: Update CLAUDE.md**

In the **Environment** block, add after the `JOB_HOST` line:

```
JOB_API_TOKEN       bearer token for the local write API (host env file only)
JOB_API_PORT        write API port (default 8765)
```

In the **Architecture** section, after the `job_ingest.py` paragraph, add:

```
`jobapi.py` is the local write API (FastAPI, 127.0.0.1, bearer token) that
the lead inbox UI's lead inbox writes through. Every endpoint is a thin wrapper
over an audited `jobdb.py` setter, so `jobdb.py` stays the only writer and the
state machine lives in one language. `GET /jobs/{ident}/transitions` exists so
the UI can render only legal actions without duplicating `TRANSITIONS`. It has
no endpoint that submits, fills, or logs in, and must never get one. Runbook:
deploy/README-job-api.md.
```

In the **Commands** section, after the `state` line, add:

```
read/unread             a lead is unread until the operator disposes of it (read_at IS
                        NULL). Set from the lead inbox UI inbox, not the CLI.
```

- [ ] **Step 6: Verify the docs claim nothing false**

Run: `grep -n "8765\|JOB_API" CLAUDE.md deploy/README-job-api.md deploy/job-api.service bin/job-api.sh`
Expected: the port and env var names agree across all four files.

- [ ] **Step 7: Run the full suite one last time**

Run: `./.venv/bin/python -m pytest -q`
Expected: PASS, with the 27 new tests from this plan included.

- [ ] **Step 8: Commit**

```bash
git add bin/job-api.sh deploy/job-api.service deploy/README-job-api.md CLAUDE.md
git commit -m "[deploy]: systemd unit and runbook for the write API

Documents the deploy order that matters: the API has to be live before the
the lead inbox UI build that calls it."
```

---

## Deployment (after the PR merges)

Not part of the plan's tasks, but the reason several of them are shaped the way they are.

0. **Rehearse the migration on a copy of the real database.** The unit tests
   use a synthetic pre-inbox schema; this is the one check against the actual
   411 rows, and it is cheap.

   ```bash
   scp $JOB_HOST:~/job-hound/jobs.db /tmp/jobs-rehearsal.db
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
   backfill did not cover the existing rows. Delete the copy afterwards; a
   stray jobs.db on the Mac is the exact bug docs/single-source-of-truth.md
   exists to prevent.

1. **Back up the database first.** The read-at backfill is one-way.
   `ssh $JOB_HOST 'cp ~/job-hound/jobs.db ~/job-hound/jobs.db.pre-inbox'`
2. `git pull --ff-only origin main` on the host, then
   `.venv/bin/pip install -r requirements.txt`.
3. Add `JOB_API_TOKEN` to `~/.config/job-hound/job-hound.env`.
4. Install and start `job-api.service` per the runbook.
5. **Verify the lead inbox UI still reads the database.** This is the step that
   catches the one real deployment risk: `better-sqlite3` opens the file with
   `readonly: true`, and a read-only connection to a WAL database needs access
   to the `-shm` file. Both processes run as the same user so it should be
   fine, but confirm the jobs tab still loads before calling this done.
6. `bin/jh list --state queued` from the Mac, to confirm the CLI still works
   against a WAL database.

## Follow-on

The lead inbox UI half (the inbox UI, the API client, the proxy routes, and
retiring `lib/job-votes.ts`) gets its own plan, written against the HTTP
contract this one produces once it is real. See the spec's rollout section.

## As built: deviations from this plan

The body above is left as written, as the historical record. What actually
shipped differs in these ways, and the follow-on plan should copy the shipped
version rather than the plan's.

- **The WAL concurrency test uses `BEGIN EXCLUSIVE`, not the `BEGIN IMMEDIATE`
  shown at line 504.** `BEGIN IMMEDIATE` takes only a RESERVED lock, which does
  not block readers under any journal mode, so that test proved nothing: it
  passed with WAL removed.
- **The `timeout=30.0` on `sqlite3.connect` at line 525 is gone.** It set the
  same busy timeout that the next line's `PRAGMA busy_timeout = 5000`
  immediately overwrote, so it read as a 30-second wait that was really 5. The
  pragma is now the only thing setting it.
- **The read/unread note lives in prose after the Commands block in CLAUDE.md,
  not inside it as line 1131 directs.** Read state is not a command, and the
  block is a command list.
- **The test counts in this plan are wrong.** Line 1146 expects 27 new tests;
  the plan's six tasks produced 35, and the fix wave below took it to 41. Line
  225 expects 5 in `test_read_at_migration.py`; there are 4. Line 39 also
  attributes the 409 semantics to `test_jobapi.py`; they live in
  `test_jobapi_state.py`, a file the plan does not name at all.

A whole-branch review after execution then added a fix wave on top: the fit
corpus normalizes `notes` before rendering it, `jobapi.py` caps `vote_note` at
280, the `state_log` note summary is capped at 120, the deploy steps moved into
`deploy/README-job-api.md`, and the spec gained the 422 and 503 responses plus
the guidance that the inbox offers only the triage transitions.
