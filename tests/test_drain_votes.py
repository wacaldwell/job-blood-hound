import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import job_ingest
import jobdb


def vote_file(base, name, payload):
    root = Path(base) / "votes"
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(json.dumps(payload) if isinstance(payload, dict)
                             else payload)


class DrainVotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name) / "job-inbox"
        self.db = jobdb.JobDB(Path(self.tmp.name) / "test.db")
        self.db.upsert_job({"id": "1", "title": "Platform Engineer",
                            "location": "Remote", "url": "https://x",
                            "company": "acme", "ats": "greenhouse"})
        row = self.db.conn.execute("SELECT * FROM jobs").fetchone()
        self.uid, self.slug = row["uid"], row["slug"]

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_empty_or_absent_spool_is_noop(self):
        self.assertEqual(job_ingest.drain_votes(self.db, self.inbox), 0)

    def test_happy_path_applies_and_archives(self):
        vote_file(self.inbox, f"1000-{self.slug}.json",
                  {"slug": self.slug, "vote": "up", "note": "great fit",
                   "voted_at": "2026-07-06T12:00:00Z"})
        applied = job_ingest.drain_votes(self.db, self.inbox)
        self.assertEqual(applied, 1)
        row = self.db.get(self.uid)
        self.assertEqual(row["vote"], "up")
        self.assertEqual(row["vote_note"], "great fit")
        processed = list((self.inbox / "votes" / "processed").glob("*.json"))
        self.assertEqual(len(processed), 1)
        self.assertEqual(list((self.inbox / "votes").glob("*.json")), [])

    def test_last_write_wins_by_filename_order(self):
        vote_file(self.inbox, f"1000-{self.slug}.json",
                  {"slug": self.slug, "vote": "up", "voted_at": "t1"})
        vote_file(self.inbox, f"2000-{self.slug}.json",
                  {"slug": self.slug, "vote": "down", "note": "changed my mind",
                   "voted_at": "t2"})
        self.assertEqual(job_ingest.drain_votes(self.db, self.inbox), 2)
        self.assertEqual(self.db.get(self.uid)["vote"], "down")

    def test_malformed_json_quarantined(self):
        vote_file(self.inbox, "1000-bad.json", "{not json")
        self.assertEqual(job_ingest.drain_votes(self.db, self.inbox), 0)
        self.assertEqual(len(list((self.inbox / "votes" / "failed").glob("*.json"))), 1)

    def test_unknown_slug_quarantined(self):
        vote_file(self.inbox, "1000-ghost.json",
                  {"slug": "ghost__job__zzzz", "vote": "up", "voted_at": "t"})
        self.assertEqual(job_ingest.drain_votes(self.db, self.inbox), 0)
        self.assertEqual(len(list((self.inbox / "votes" / "failed").glob("*.json"))), 1)

    def test_invalid_vote_value_quarantined(self):
        vote_file(self.inbox, f"1000-{self.slug}.json",
                  {"slug": self.slug, "vote": "sideways", "voted_at": "t"})
        self.assertEqual(job_ingest.drain_votes(self.db, self.inbox), 0)
        self.assertIsNone(self.db.get(self.uid)["vote"])
        self.assertEqual(len(list((self.inbox / "votes" / "failed").glob("*.json"))), 1)

    def test_move_failure_does_not_raise(self):
        vote_file(self.inbox, f"1000-{self.slug}.json",
                  {"slug": self.slug, "vote": "up", "voted_at": "t"})
        with mock.patch("pathlib.Path.replace", side_effect=OSError("disk full")):
            applied = job_ingest.drain_votes(self.db, self.inbox)
        self.assertEqual(applied, 1)
        self.assertEqual(self.db.get(self.uid)["vote"], "up")


if __name__ == "__main__":
    unittest.main()
