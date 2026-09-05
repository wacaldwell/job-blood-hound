import tempfile
import unittest
from pathlib import Path

import fit
import jobdb


class HistoryVoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = jobdb.JobDB(Path(self.tmp.name) / "t.db")
        for i, company in enumerate(["acme", "globex", "initech"]):
            self.db.upsert_job({"id": str(i), "title": f"Platform Engineer {i}",
                                "location": "Remote", "url": "https://x",
                                "company": company, "ats": "greenhouse"})

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def uid_for(self, company):
        return self.db.conn.execute(
            "SELECT uid FROM jobs WHERE company = ?", (company,)).fetchone()["uid"]

    def test_upvote_appears_as_liked_with_reason(self):
        self.db.set_vote(self.uid_for("acme"), "up", note="love the domain")
        hist = fit.build_history(self.db)
        entry = next(h for h in hist if h["company"] == "acme")
        self.assertEqual(entry["decision"], "liked")
        self.assertEqual(entry["reason"], "love the domain")

    def test_downvote_appears_as_disliked(self):
        self.db.set_vote(self.uid_for("globex"), "down", note="too much K8s")
        hist = fit.build_history(self.db)
        entry = next(h for h in hist if h["company"] == "globex")
        self.assertEqual(entry["decision"], "disliked")
        self.assertEqual(entry["reason"], "too much K8s")

    def test_unvoted_discovered_jobs_stay_excluded(self):
        self.db.set_vote(self.uid_for("acme"), "up")
        hist = fit.build_history(self.db)
        self.assertNotIn("initech", [h["company"] for h in hist])

    def test_lifecycle_decision_beats_vote(self):
        # A skipped job with a stray vote still reads as rejected: lifecycle
        # decisions are the stronger signal.
        uid = self.uid_for("acme")
        self.db.set_vote(uid, "up")
        self.db.set_state(uid, "skipped")
        hist = fit.build_history(self.db)
        entry = next(h for h in hist if h["company"] == "acme")
        self.assertEqual(entry["decision"], "rejected")


if __name__ == "__main__":
    unittest.main()
