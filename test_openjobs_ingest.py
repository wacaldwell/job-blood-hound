"""The ingest half of the wide net: dedup, the per-run cap, and provenance.

openjobs.discover has no database knowledge on purpose. Everything that needs
to know what is already in the pipeline lives here.
"""
import jobdb
import job_cli


CFG = {"title_terms": ["engineer", "director", "infrastructure"],
       "exclude_terms": [], "location_terms": ["remote"]}


def cand(n, sim, **over):
    j = {"id": str(n), "title": f"Infrastructure Engineer {n}",
         "location": "Remote", "url": f"https://example.com/{n}",
         "company": f"co{n}", "company_display": f"Co {n}",
         "ats": "greenhouse", "posted_at": "2026-08-01",
         "date_source": "openjobs:first_seen~", "description": "AWS.",
         "location_type": "remote", "source": "openjobs", "sim": sim}
    j.update(over)
    return j


def db_at(tmp_path):
    return jobdb.JobDB(tmp_path / "jobs.db")


def test_ingests_candidates_as_discovered_with_their_source(tmp_path):
    db = db_at(tmp_path)
    r = job_cli.openjobs_and_ingest(db, CFG, top=5,
                                    discover=lambda *a, **k: [cand(1, 0.9)])
    assert r["added"] == 1
    row = db.get("greenhouse:co1:1")
    assert row["state"] == "discovered"
    assert row["source"] == "openjobs"
    assert row["description"] == "AWS."


def test_cap_is_applied_and_takes_the_best_matches(tmp_path):
    db = db_at(tmp_path)
    found = [cand(1, 0.9), cand(2, 0.8), cand(3, 0.7), cand(4, 0.6)]
    r = job_cli.openjobs_and_ingest(db, CFG, top=2,
                                    discover=lambda *a, **k: found)
    assert r["added"] == 2
    assert db.get("greenhouse:co1:1") is not None
    assert db.get("greenhouse:co2:2") is not None
    assert db.get("greenhouse:co4:4") is None


def test_a_posting_the_scanner_already_found_is_not_re_ingested(tmp_path):
    """Both crawlers read the same boards, so uid collision is the common case
    and must cost nothing."""
    db = db_at(tmp_path)
    db.upsert_job({"id": "1", "title": "Infrastructure Engineer 1",
                   "location": "Remote", "url": "https://example.com/1",
                   "company": "co1", "ats": "greenhouse", "source": "scan"})
    r = job_cli.openjobs_and_ingest(db, CFG, top=5,
                                    discover=lambda *a, **k: [cand(1, 0.9)])
    assert r["added"] == 0
    assert r["duplicate"] == 1
    assert db.get("greenhouse:co1:1")["source"] == "scan", "must not be overwritten"


def test_dedup_also_catches_the_same_url_under_a_different_id(tmp_path):
    """Where two crawlers spell an ATS id or slug differently, the canonical
    posting URL still matches. uid dedup alone would ingest a twin."""
    db = db_at(tmp_path)
    db.upsert_job({"id": "other", "title": "Infrastructure Engineer 1",
                   "location": "Remote", "url": "https://example.com/1",
                   "company": "different-slug", "ats": "lever"})
    r = job_cli.openjobs_and_ingest(db, CFG, top=5,
                                    discover=lambda *a, **k: [cand(1, 0.9)])
    assert r["added"] == 0
    assert r["duplicate"] == 1


def test_the_cap_buys_new_leads_not_slots_filled_by_duplicates(tmp_path):
    """Dedup runs BEFORE the cap. Capping first would mean a day where the top
    15 are all already known ingests nothing at all."""
    db = db_at(tmp_path)
    for n in (1, 2):
        db.upsert_job({"id": str(n), "title": "x", "location": "Remote",
                       "url": f"https://example.com/{n}", "company": f"co{n}",
                       "ats": "greenhouse"})
    found = [cand(1, 0.9), cand(2, 0.8), cand(3, 0.7), cand(4, 0.6)]
    r = job_cli.openjobs_and_ingest(db, CFG, top=2,
                                    discover=lambda *a, **k: found)
    assert r["added"] == 2
    assert db.get("greenhouse:co3:3") is not None
    assert db.get("greenhouse:co4:4") is not None


def test_a_failed_wide_net_reports_zero_and_does_not_raise(tmp_path):
    """discover() already swallows its own failures; this is the belt to that
    braces. bin/daily.sh must reach the digest no matter what."""
    def boom(*a, **k):
        raise RuntimeError("worker down")

    db = db_at(tmp_path)
    r = job_cli.openjobs_and_ingest(db, CFG, top=5, discover=boom)
    assert r["added"] == 0
    assert r["error"]


def test_url_dedup_ignores_blank_urls(tmp_path):
    """A row with no URL must not swallow every candidate that also has none."""
    db = db_at(tmp_path)
    db.upsert_job({"id": "9", "title": "x", "location": "Remote", "url": "",
                   "company": "co9", "ats": "greenhouse"})
    r = job_cli.openjobs_and_ingest(
        db, CFG, top=5, discover=lambda *a, **k: [cand(1, 0.9, url="")])
    assert r["added"] == 1


def test_top_zero_means_zero_not_unlimited(tmp_path):
    """OPENJOBS_TOP=0 in bin/daily.sh reads as "ingest nothing today". The
    truthiness check made it "ingest everything", which on a 12-group slice is
    ~250 rows into the single system of record."""
    db = db_at(tmp_path)
    found = [cand(n, 1.0 - n / 100) for n in range(1, 6)]
    r = job_cli.openjobs_and_ingest(db, CFG, top=0,
                                    discover=lambda *a, **k: found)
    assert r["added"] == 0
    assert r["capped"] == 5


def test_url_dedup_ignores_a_tracking_parameter(tmp_path):
    db = db_at(tmp_path)
    db.upsert_job({"id": "1", "title": "x", "location": "Remote",
                   "url": "https://job-boards.greenhouse.io/federato/jobs/53825",
                   "company": "federato", "ats": "greenhouse"})
    r = job_cli.openjobs_and_ingest(
        db, CFG, top=5, discover=lambda *a, **k: [cand(
            9, 0.9, url="https://job-boards.greenhouse.io/federato/jobs/53825?gh_src=abc")])
    assert r["duplicate"] == 1, "a tracking parameter created a twin"


def test_url_dedup_survives_the_greenhouse_board_host_rename(tmp_path):
    """Greenhouse moved boards from boards.greenhouse.io to
    job-boards.greenhouse.io and both spellings are still in circulation, so
    the DB and the corpus routinely disagree on the host for the same posting.
    This is the exact case layer 2 exists for."""
    db = db_at(tmp_path)
    db.upsert_job({"id": "1", "title": "x", "location": "Remote",
                   "url": "https://boards.greenhouse.io/federato/jobs/53825",
                   "company": "federato", "ats": "greenhouse"})
    r = job_cli.openjobs_and_ingest(
        db, CFG, top=5, discover=lambda *a, **k: [cand(
            9, 0.9, url="https://job-boards.greenhouse.io/federato/jobs/53825")])
    assert r["duplicate"] == 1


def test_url_dedup_ignores_scheme_case_and_trailing_slash(tmp_path):
    db = db_at(tmp_path)
    db.upsert_job({"id": "1", "title": "x", "location": "Remote",
                   "url": "https://www.Example.com/jobs/7/", "company": "co",
                   "ats": "greenhouse"})
    r = job_cli.openjobs_and_ingest(
        db, CFG, top=5,
        discover=lambda *a, **k: [cand(9, 0.9, url="http://example.com/jobs/7")])
    assert r["duplicate"] == 1


def test_two_genuinely_different_postings_are_not_collapsed(tmp_path):
    """Canonicalisation must not become its own false-dedup bug."""
    db = db_at(tmp_path)
    db.upsert_job({"id": "1", "title": "x", "location": "Remote",
                   "url": "https://example.com/jobs/7", "company": "co",
                   "ats": "greenhouse"})
    r = job_cli.openjobs_and_ingest(
        db, CFG, top=5,
        discover=lambda *a, **k: [cand(9, 0.9, url="https://example.com/jobs/8")])
    assert r["added"] == 1


def test_the_cap_takes_the_best_matches_even_if_discover_returns_unsorted(tmp_path):
    """The cap slices the front of the list, so it depends on an ordering it
    does not itself impose. That was an unwritten contract between two
    functions, and it is the same shape as the centroid bug already fixed
    once."""
    db = db_at(tmp_path)
    found = [cand(1, 0.10), cand(2, 0.99), cand(3, 0.50)]
    job_cli.openjobs_and_ingest(db, CFG, top=1, discover=lambda *a, **k: found)
    assert db.get("greenhouse:co2:2") is not None, "did not take the best match"


def test_two_spellings_of_one_posting_in_the_SAME_run_collapse(tmp_path):
    """known_urls starts as a snapshot of what is already stored, so without
    feeding accepted URLs back in, a posting the corpus lists twice under two
    hosts ingests twice on its first sighting."""
    db = db_at(tmp_path)
    both = [cand(1, 0.9, url="https://boards.greenhouse.io/federato/jobs/53825"),
            cand(2, 0.8, url="https://job-boards.greenhouse.io/federato/jobs/53825")]
    r = job_cli.openjobs_and_ingest(db, CFG, top=5, discover=lambda *a, **k: both)
    assert r["added"] == 1
    assert r["duplicate"] == 1
