"""Four processes open this database on the host: the write API, the nightly
scan, the 5-minute ingest timer, and bin/jh. The lead inbox opens it a fifth
time, read-only. In the default rollback journal a writer blocks every reader,
which with a synchronous API in the mix means the UI hangs behind the scan. WAL
is what makes concurrent access boring.
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
    """With WAL, a reader can fetch while a writer holds an exclusive lock.
    Without WAL, the reader blocks with 'database is locked' and waits out the
    busy timeout before raising."""
    path = tmp_path / "t.db"
    writer = jobdb.JobDB(path)
    writer.upsert_job(_job(1))
    uid = jobdb.make_job_uid("greenhouse", "acme", "1")

    reader = jobdb.JobDB(path)
    writer.conn.execute("BEGIN EXCLUSIVE")
    writer.conn.execute(
        "UPDATE jobs SET title = 'changed' WHERE uid = ?", (uid,))

    assert reader.get(uid)["title"] == "Senior SRE"

    writer.conn.rollback()
    reader.close()
    writer.close()
