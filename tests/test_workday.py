import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests

import job_monitor as jm


def _today():
    return datetime.now(timezone.utc).date()


def posting(title="VP, Finance", external_path="/job/Atlanta-GA/VP--Finance_R3268",
            locations_text="2 Locations", posted_on="Posted 13 Days Ago"):
    return {
        "title": title,
        "externalPath": external_path,
        "locationsText": locations_text,
        "postedOn": posted_on,
        "bulletFields": ["R3268"],
    }


def page(postings, total):
    return {"total": total, "jobPostings": postings}


def mock_post_factory(pages_by_offset):
    """Return a fake requests.post that serves a payload per offset value."""
    def fake_post(url, json=None, headers=None, timeout=None):
        offset = (json or {}).get("offset", 0)
        resp = mock.Mock()
        resp.json.return_value = pages_by_offset[offset]
        resp.raise_for_status.return_value = None
        return resp
    return fake_post


class WorkdayDerivationTests(unittest.TestCase):
    def test_url_tenant_site_derivation(self):
        slug = "waystar.wd1.myworkdayjobs.com/Waystar"
        pages = {0: page([posting()], 1)}
        with mock.patch.object(jm.SESSION, "post", side_effect=mock_post_factory(pages)) as m:
            jobs = jm.fetch_workday(slug)
        # The cxs endpoint must be built from host/tenant/site.
        called_url = m.call_args_list[0].args[0]
        self.assertEqual(
            called_url,
            "https://waystar.wd1.myworkdayjobs.com/wday/cxs/waystar/Waystar/jobs",
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            jobs[0]["url"],
            "https://waystar.wd1.myworkdayjobs.com/Waystar/job/Atlanta-GA/VP--Finance_R3268",
        )

    def test_normalized_dict_has_all_keys(self):
        slug = "waystar.wd1.myworkdayjobs.com/Waystar"
        pages = {0: page([posting()], 1)}
        with mock.patch.object(jm.SESSION, "post", side_effect=mock_post_factory(pages)):
            jobs = jm.fetch_workday(slug)
        j = jobs[0]
        for key in ("id", "title", "location", "url", "company", "ats",
                    "posted_at", "date_source"):
            self.assertIn(key, j)
        self.assertEqual(j["ats"], "workday")
        self.assertEqual(j["id"], "/job/Atlanta-GA/VP--Finance_R3268")
        self.assertEqual(j["company"], slug)
        self.assertEqual(j["date_source"], "workday:postedOn~")


class WorkdayPaginationTests(unittest.TestCase):
    def test_paginates_until_total(self):
        # total=45 -> offsets 0, 20, 40 (20 + 20 + 5). Real Workday reports the
        # total on the FIRST page only and sends total=0 on later pages, so the
        # fetcher must remember the first-page total, not re-read it each page.
        p1 = [posting(external_path=f"/job/Atlanta-GA/R{i}") for i in range(20)]
        p2 = [posting(external_path=f"/job/Atlanta-GA/R{i}") for i in range(20, 40)]
        p3 = [posting(external_path=f"/job/Atlanta-GA/R{i}") for i in range(40, 45)]
        pages = {0: page(p1, 45), 20: page(p2, 0), 40: page(p3, 0)}
        with mock.patch("job_monitor.time.sleep"), \
             mock.patch.object(jm.SESSION, "post", side_effect=mock_post_factory(pages)) as m:
            jobs = jm.fetch_workday("waystar.wd1.myworkdayjobs.com/Waystar")
        self.assertEqual(len(jobs), 45)
        offsets = [c.kwargs["json"]["offset"] for c in m.call_args_list]
        self.assertEqual(offsets, [0, 20, 40])

    def test_stops_when_no_postings_returned(self):
        # Defensive: an empty page ends pagination even if total lies.
        pages = {0: page([], 999)}
        with mock.patch("job_monitor.time.sleep"), \
             mock.patch.object(jm.SESSION, "post", side_effect=mock_post_factory(pages)):
            jobs = jm.fetch_workday("waystar.wd1.myworkdayjobs.com/Waystar")
        self.assertEqual(jobs, [])


class WorkdayDateTests(unittest.TestCase):
    def fetch_one(self, posted_on):
        pages = {0: page([posting(posted_on=posted_on)], 1)}
        with mock.patch.object(jm.SESSION, "post", side_effect=mock_post_factory(pages)):
            return jm.fetch_workday("waystar.wd1.myworkdayjobs.com/Waystar")[0]

    def test_posted_today(self):
        j = self.fetch_one("Posted Today")
        self.assertEqual(j["posted_at"], _today().isoformat())

    def test_posted_yesterday(self):
        j = self.fetch_one("Posted Yesterday")
        self.assertEqual(j["posted_at"], (_today() - timedelta(days=1)).isoformat())

    def test_posted_n_days_ago(self):
        j = self.fetch_one("Posted 5 Days Ago")
        self.assertEqual(j["posted_at"], (_today() - timedelta(days=5)).isoformat())

    def test_posted_n_plus_days_ago(self):
        j = self.fetch_one("Posted 30+ Days Ago")
        self.assertEqual(j["posted_at"], (_today() - timedelta(days=30)).isoformat())

    def test_unparseable_date_is_empty(self):
        j = self.fetch_one("Reposted recently")
        self.assertEqual(j["posted_at"], "")
        # date_source still records the approximate provenance.
        self.assertEqual(j["date_source"], "workday:postedOn~")


class WorkdayLocationTests(unittest.TestCase):
    def fetch_one(self, locations_text, external_path):
        pages = {0: page([posting(locations_text=locations_text,
                                  external_path=external_path)], 1)}
        with mock.patch.object(jm.SESSION, "post", side_effect=mock_post_factory(pages)):
            return jm.fetch_workday("waystar.wd1.myworkdayjobs.com/Waystar")[0]

    def test_concrete_location_passes_through(self):
        j = self.fetch_one("Atlanta, GA", "/job/Atlanta-GA/R1")
        self.assertEqual(j["location"], "Atlanta, GA")

    def test_summary_location_falls_back_to_path(self):
        j = self.fetch_one("2 Locations", "/job/Atlanta-GA/VP--Finance_R3268")
        self.assertEqual(j["location"], "Atlanta GA")

    def test_empty_location_falls_back_to_path(self):
        j = self.fetch_one("", "/job/Remote-Illinois-USA/R9")
        self.assertEqual(j["location"], "Remote Illinois USA")


class WorkdaySearchTextTests(unittest.TestCase):
    def test_default_search_text_is_empty(self):
        pages = {0: page([posting()], 1)}
        with mock.patch.object(jm.SESSION, "post", side_effect=mock_post_factory(pages)) as m:
            jm.fetch_workday("waystar.wd1.myworkdayjobs.com/Waystar")
        self.assertEqual(m.call_args_list[0].kwargs["json"]["searchText"], "")

    def test_search_text_sent_in_body(self):
        pages = {0: page([posting()], 1)}
        with mock.patch.object(jm.SESSION, "post", side_effect=mock_post_factory(pages)) as m:
            jm.fetch_workday("globex.wd5.myworkdayjobs.com/GBX_External_CS",
                             search_text="engineer")
        self.assertEqual(m.call_args_list[0].kwargs["json"]["searchText"], "engineer")

    def test_run_scan_passes_company_search_text(self):
        # Huge tenants (national retailers, big banks) are only usable with a server-side
        # narrowing term; run_scan must forward it to the workday fetcher.
        cfg = {
            "title_terms": ["finance"],
            "companies": [
                {"name": "Waystar", "ats": "workday",
                 "slug": "waystar.wd1.myworkdayjobs.com/Waystar",
                 "search_text": "engineer"},
            ],
        }
        pages = {0: page([posting()], 1)}
        with mock.patch("job_monitor.time.sleep"), \
             mock.patch.object(jm.SESSION, "post", side_effect=mock_post_factory(pages)) as m:
            jm.run_scan(cfg, set())
        self.assertEqual(m.call_args_list[0].kwargs["json"]["searchText"], "engineer")


class WorkdayErrorTests(unittest.TestCase):
    def test_http_error_propagates(self):
        # A non-200 must raise so run_scan's handler skips the company.
        resp = mock.Mock()
        resp.raise_for_status.side_effect = requests.HTTPError("403")
        with mock.patch.object(jm.SESSION, "post", return_value=resp):
            with self.assertRaises(requests.HTTPError):
                jm.fetch_workday("waystar.wd1.myworkdayjobs.com/Waystar")

    def test_non_json_response_raises(self):
        # An HTML body (json() raises) must surface as an exception so the
        # per-company try/except in run_scan skips it cleanly.
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.side_effect = ValueError("no json")
        with mock.patch.object(jm.SESSION, "post", return_value=resp):
            with self.assertRaises(ValueError):
                jm.fetch_workday("waystar.wd1.myworkdayjobs.com/Waystar")


class WorkdayIntegrationTests(unittest.TestCase):
    def test_registered_in_fetchers(self):
        self.assertIn("workday", jm.FETCHERS)
        self.assertIs(jm.FETCHERS["workday"], jm.fetch_workday)

    def test_workday_not_manual(self):
        self.assertNotIn("workday", jm.MANUAL_ATS)
        self.assertIn("icims", jm.MANUAL_ATS)

    def test_workday_without_slug_is_manual(self):
        # A workday company with no usable slug must degrade to manual, never crash.
        cfg = {
            "title_terms": ["engineer"],
            "companies": [
                {"name": "NoSlugCo", "ats": "workday",
                 "careers_url": "https://example.com/careers"},
            ],
        }
        new_jobs, all_matches, manual = jm.run_scan(cfg, set())
        self.assertEqual(all_matches, [])
        self.assertEqual([m["name"] for m in manual], ["NoSlugCo"])

    def test_workday_with_slug_is_scanned(self):
        cfg = {
            "title_terms": ["finance"],
            "companies": [
                {"name": "Waystar", "ats": "workday",
                 "slug": "waystar.wd1.myworkdayjobs.com/Waystar"},
            ],
        }
        pages = {0: page([posting(title="VP, Finance",
                                  locations_text="Remote",
                                  posted_on="Posted Today")], 1)}
        with mock.patch("job_monitor.time.sleep"), \
             mock.patch.object(jm.SESSION, "post", side_effect=mock_post_factory(pages)):
            new_jobs, all_matches, manual = jm.run_scan(cfg, set())
        self.assertEqual(manual, [])
        self.assertEqual(len(all_matches), 1)
        self.assertEqual(all_matches[0]["company_display"], "Waystar")


if __name__ == "__main__":
    unittest.main()
