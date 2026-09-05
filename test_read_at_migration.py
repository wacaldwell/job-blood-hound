"""The unread queue depends entirely on read_at being NULL for new leads.

The backfill that marks every pre-inbox lead read must fire exactly once, at
the migration that adds the column. A backfill in the body of _migrate would
re-run on every JobDB open (the scan opens it daily, the ingest timer every
five minutes) and mark each newly discovered lead read before the human ever saw
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
    location TEXT, url TEXT, posted_at TEXT, date_source TEXT,
    description TEXT,
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
