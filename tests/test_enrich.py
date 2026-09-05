import unittest
from unittest import mock
import job_monitor as jm

CFG = {
    "title_terms": ["site reliability", "platform engineer"],
    "location_terms": jm.REMOTE_TERMS + ["charlotte"],
    "exclude_terms": ["sales"],
    "companies": [
        {"name": "Acme Co", "ats": "greenhouse", "slug": "acme",
         "category": "mid_market"},
    ],
}

FAKE_JOBS = [
    {"id": "1", "title": "Senior Site Reliability Engineer", "location": "Remote",
     "url": "u1", "company": "acme", "ats": "greenhouse",
     "posted_at": "", "date_source": ""},
    {"id": "2", "title": "Platform Engineer", "location": "Charlotte, NC",
     "url": "u2", "company": "acme", "ats": "greenhouse",
     "posted_at": "", "date_source": ""},
]


class EnrichTests(unittest.TestCase):
    def test_enrichment_fields(self):
        fake = lambda slug: [dict(j) for j in FAKE_JOBS]
        with mock.patch.dict(jm.FETCHERS, {"greenhouse": fake}):
            new, all_matches, manual = jm.run_scan(CFG, seen=set())
        self.assertEqual(len(all_matches), 2)
        by_id = {j["id"]: j for j in all_matches}
        # company slug preserved (load-bearing); display name added separately
        self.assertEqual(by_id["1"]["company"], "acme")
        self.assertEqual(by_id["1"]["company_display"], "Acme Co")
        self.assertEqual(by_id["1"]["category"], "mid_market")
        self.assertEqual(by_id["1"]["location_type"], "remote")
        self.assertEqual(by_id["2"]["location_type"], "onsite/hybrid")


if __name__ == "__main__":
    unittest.main()
