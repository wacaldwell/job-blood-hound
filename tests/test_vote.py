import tempfile
import unittest
from pathlib import Path

import jobdb


def make_db(tmpdir):
    db = jobdb.JobDB(Path(tmpdir) / "test.db")
    db.upsert_job({"id": "123", "title": "Platform Engineer", "location": "Remote",
                   "url": "https://example.com/j/123", "company": "acme",
                   "ats": "greenhouse"})
    row = db.conn.execute("SELECT * FROM jobs").fetchone()
    return db, row["uid"]


class SetVoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db, self.uid = make_db(self.tmp.name)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_set_vote_up_with_note(self):
        row = self.db.set_vote(self.uid, "up", note="love the domain")
        self.assertEqual(row["vote"], "up")
        self.assertEqual(row["vote_note"], "love the domain")
        self.assertTrue(row["voted_at"])

    def test_vote_overwrites_previous(self):
        self.db.set_vote(self.uid, "up")
        row = self.db.set_vote(self.uid, "down", note="too much K8s")
        self.assertEqual(row["vote"], "down")
        self.assertEqual(row["vote_note"], "too much K8s")

    def test_clear_vote(self):
        self.db.set_vote(self.uid, "up", note="x")
        row = self.db.set_vote(self.uid, None)
        self.assertIsNone(row["vote"])
        self.assertIsNone(row["vote_note"])
        self.assertIsNone(row["voted_at"])

    def test_vote_does_not_change_state(self):
        row = self.db.set_vote(self.uid, "down")
        self.assertEqual(row["state"], "discovered")

    def test_vote_writes_state_log_audit(self):
        self.db.set_vote(self.uid, "down", note="wrong stack")
        log = self.db.conn.execute(
            "SELECT * FROM state_log WHERE job_uid = ? ORDER BY id DESC",
            (self.uid,)).fetchone()
        self.assertEqual(log["from_state"], "discovered")
        self.assertEqual(log["to_state"], "discovered")
        self.assertIn("vote: down", log["note"])
        self.assertIn("wrong stack", log["note"])

    def test_invalid_vote_rejected(self):
        with self.assertRaises(ValueError):
            self.db.set_vote(self.uid, "sideways")

    def test_unknown_uid_rejected(self):
        with self.assertRaises(ValueError):
            self.db.set_vote("nope:nope:nope", "up")


class VoteMigrationTests(unittest.TestCase):
    def test_old_db_gains_vote_columns(self):
        # Simulate a DB created before the vote columns existed, then confirm
        # opening it with JobDB migrates the columns in without data loss.
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(str(path))
            conn.execute(
                "CREATE TABLE jobs ("
                "uid TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL,"
                "ext_id TEXT NOT NULL, ats TEXT NOT NULL, company TEXT NOT NULL,"
                "title TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'discovered',"
                "discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute(
                "INSERT INTO jobs (uid, slug, ext_id, ats, company, title,"
                " state, discovered_at, updated_at) VALUES"
                " ('a:b:1', 'b__x__1', '1', 'a', 'b', 'X', 'discovered',"
                " '2026-01-01', '2026-01-01')")
            conn.commit()
            conn.close()
            db = jobdb.JobDB(path)
            cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(jobs)")}
            self.assertIn("vote", cols)
            self.assertIn("vote_note", cols)
            self.assertIn("voted_at", cols)
            self.assertEqual(db.conn.execute(
                "SELECT COUNT(*) AS n FROM jobs").fetchone()["n"], 1)
            db.close()


if __name__ == "__main__":
    unittest.main()
