import job_ingest
import job_fetch
from unittest import mock
import job_monitor
import gate
import json
from pathlib import Path
import os
import pytest
from jobdb import JobDB, make_job_uid


@pytest.fixture
def db(tmp_path):
    return JobDB(str(tmp_path / "jobs.db"))


def _entry():
    return {"id": "e1", "url": "https://boards.greenhouse.io/acme/jobs/1",
            "note": "", "submitted_at": "t0"}


def test_process_one_high_score_drafts(db, monkeypatch):
    monkeypatch.setattr(job_ingest, "fetch_posting_meta", lambda parsed: {
        "title": "Staff SRE", "location": "Remote", "description": "JD BODY"})
    calls = {}
    def fake_verdict(row, master, history, api_key, jd_text=None):
        calls["verdict_jd"] = jd_text
        return {"llm_fit_score": 95, "llm_rationale": "great fit", "llm_coding_bar": "light"}
    def fake_generate(db_, row, **kw):
        calls["gen_kw"] = kw
        return {"version": 1, "files": {"resume_docx": "/pkg/resume.docx",
                                         "cover_docx": "/pkg/cover.docx"}, "pdf": False}
    pings = []
    rec = job_ingest.process_one(
        _entry(), db, master={"contact": {"name": "A"}}, api_key="k", webhook="http://hook",
        master_path="/m/master.yaml",
        verdict_fn=fake_verdict, generate_fn=fake_generate,
        gate_fn=lambda *a, **k: {"decision": gate.PROCEED, "report_path": "/tmp/r.md"},
        discord_fn=lambda url, text, **kw: pings.append(text) or True)

    assert rec["status"] == "drafted"
    assert rec["state"] == "drafted"
    assert rec["llm_fit_score"] == 95
    assert rec["uid"] == make_job_uid("greenhouse", "acme", "1")
    assert {"kind": "resume_docx", "path": "/pkg/resume.docx"} in rec["package_files"]
    assert calls["verdict_jd"] == "JD BODY"       # JD passed through, no double fetch
    assert calls["gen_kw"].get("master_path") == "/m/master.yaml"  # master threaded to generate
    assert calls["gen_kw"].get("api_key") == "k"
    assert db.get(rec["uid"])["state"] == "drafted"
    assert any("ready for review" in p for p in pings)


def test_process_one_low_score_scored_only(db, monkeypatch):
    monkeypatch.setattr(job_ingest, "fetch_posting_meta", lambda parsed: {
        "title": "PM", "location": "NYC", "description": "JD"})
    gen_called = []
    rec = job_ingest.process_one(
        _entry(), db, master={}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: {"llm_fit_score": 61, "llm_rationale": "meh", "llm_coding_bar": "n/a"},
        generate_fn=lambda *a, **k: gen_called.append(1),
        discord_fn=lambda *a, **k: True)
    assert rec["status"] == "scored"
    assert rec["llm_fit_score"] == 61
    assert rec["package_files"] == []
    assert gen_called == []                         # never drafted
    assert rec["state"] == "skipped"                # moved out of the draftable pool
    assert db.get(rec["uid"])["state"] == "skipped"


def test_process_one_unsupported_url_fetch_failed(db, monkeypatch):
    def no_resolve(url):
        raise job_fetch.FetchError("stubbed: unresolvable")
    monkeypatch.setattr(job_fetch, "resolve_url", no_resolve)
    pings = []
    rec = job_ingest.process_one(
        {"id": "e2", "url": "https://acme.wd1.myworkdayjobs.com/x/job/1",
         "note": "", "submitted_at": "t"},
        db, master={}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: pytest.fail("verdict must not run"),
        generate_fn=lambda *a, **k: pytest.fail("generate must not run"),
        discord_fn=lambda url, text, **kw: pings.append(text) or True)
    assert rec["status"] == "fetch_failed"
    assert rec["uid"] is None
    assert any("by hand" in p for p in pings)


def _resp(payload):
    r = mock.Mock()
    r.json.return_value = payload
    return r


def test_fetch_posting_meta_greenhouse():
    payload = {"title": "Staff Platform Engineer",
               "location": {"name": "Remote - US"},
               "content": "<p>Build <b>things</b></p>"}
    with mock.patch.object(job_monitor.SESSION, "get", return_value=_resp(payload)):
        meta = job_ingest.fetch_posting_meta(
            {"ats": "greenhouse", "company": "acme", "ext_id": "1"})
    assert meta["title"] == "Staff Platform Engineer"
    assert meta["location"] == "Remote - US"
    assert "Build" in meta["description"]
    assert "<p>" not in meta["description"]


def test_fetch_posting_meta_lever():
    payload = {"text": "Senior SRE",
               "categories": {"location": "Remote"},
               "descriptionPlain": "Operate the platform.",
               "lists": [{"text": "<li>on-call</li>"}]}
    with mock.patch.object(job_monitor.SESSION, "get", return_value=_resp(payload)):
        meta = job_ingest.fetch_posting_meta(
            {"ats": "lever", "company": "acme", "ext_id": "u1"})
    assert meta["title"] == "Senior SRE"
    assert meta["location"] == "Remote"
    assert "Operate the platform." in meta["description"]
    assert "on-call" in meta["description"]


def test_fetch_posting_meta_unsupported_raises():
    import pytest
    # iCIMS has no public feed and so no metadata fetcher. Workday used to be
    # the example here; it has one now.
    with pytest.raises(ValueError):
        job_ingest.fetch_posting_meta({"ats": "icims", "company": "x", "ext_id": "y"})


def test_parse_greenhouse_boards_url():
    r = job_ingest.parse_posting_url("https://boards.greenhouse.io/acme/jobs/12345")
    assert r == {"ats": "greenhouse", "company": "acme", "ext_id": "12345"}


def test_parse_greenhouse_jobboards_host():
    r = job_ingest.parse_posting_url("https://job-boards.greenhouse.io/acme/jobs/98765")
    assert r == {"ats": "greenhouse", "company": "acme", "ext_id": "98765"}


def test_parse_greenhouse_embed_url():
    r = job_ingest.parse_posting_url(
        "https://boards.greenhouse.io/embed/job_app?for=acme&token=4242")
    assert r == {"ats": "greenhouse", "company": "acme", "ext_id": "4242"}


def test_parse_lever_url():
    r = job_ingest.parse_posting_url(
        "https://jobs.lever.co/acme/6f2a-uuid-1234")
    assert r == {"ats": "lever", "company": "acme", "ext_id": "6f2a-uuid-1234"}


def test_parse_unsupported_url_returns_none():
    assert job_ingest.parse_posting_url("https://careers.acme.icims.com/jobs/1/eng/job") is None
    assert job_ingest.parse_posting_url("not a url") is None


def test_parse_greenhouse_embed_spoofed_host_returns_none():
    assert job_ingest.parse_posting_url(
        "https://boards.greenhouse.io.attacker.example/embed/job_app?for=acme&token=1") is None
    assert job_ingest.parse_posting_url(
        "https://notgreenhouse.io/embed/job_app?for=acme&token=1") is None


def test_spool_roundtrip(tmp_path):
    base = str(tmp_path / "inbox")
    pdir = Path(base) / "pending"
    pdir.mkdir(parents=True)
    (pdir / "abc.json").write_text(json.dumps(
        {"id": "abc", "url": "https://x", "note": "", "submitted_at": "t"}))
    (pdir / "bad.json").write_text("{not json")

    pending = job_ingest.read_pending(base)
    assert len(pending) == 2
    by_name = {f.name: (f, e) for f, e in pending}
    assert by_name["abc.json"][1]["id"] == "abc"
    assert by_name["bad.json"][1] is None           # malformed surfaced, not dropped
    pfile = by_name["abc.json"][0]

    rec = {"id": "abc", "status": "scored"}
    dest = job_ingest.write_processed(base, rec)
    assert Path(dest).exists()
    assert json.loads(Path(dest).read_text())["status"] == "scored"

    job_ingest.remove_pending(pfile)
    assert not pfile.exists()


def test_now_iso_is_utc_isoformat():
    s = job_ingest._now_iso()
    assert "T" in s and s.endswith("+00:00")


def test_main_processes_pending_and_clears(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    (inbox / "pending").mkdir(parents=True)
    (inbox / "pending" / "e1.json").write_text(json.dumps(
        {"id": "e1", "url": "https://jobs.lever.co/acme/u1", "note": "", "submitted_at": "t"}))
    master = tmp_path / "master_resume.yaml"
    master.write_text("contact:\n  name: A\n")

    monkeypatch.setenv("JOB_INBOX_DIR", str(inbox))
    monkeypatch.setenv("JOB_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("JOB_MASTER", str(master))
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    monkeypatch.setattr(job_ingest, "fetch_posting_meta", lambda parsed: {
        "title": "SRE", "location": "Remote", "description": "JD"})
    monkeypatch.setattr(job_ingest.fit, "verdict",
                        lambda *a, **k: {"llm_fit_score": 40, "llm_rationale": "no", "llm_coding_bar": ""})
    monkeypatch.setattr(job_ingest.notify, "post_discord", lambda *a, **k: True)

    rc = job_ingest.main()
    assert rc == 0
    assert not (inbox / "pending" / "e1.json").exists()
    assert (inbox / "processed" / "e1.json").exists()
    assert json.loads((inbox / "processed" / "e1.json").read_text())["status"] == "scored"


def test_main_aborts_without_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert job_ingest.main() == 1


def test_main_isolates_error_and_still_clears(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    (inbox / "pending").mkdir(parents=True)
    (inbox / "pending" / "e1.json").write_text(json.dumps(
        {"id": "e1", "url": "https://jobs.lever.co/acme/u1", "note": "", "submitted_at": "t"}))
    master = tmp_path / "master_resume.yaml"
    master.write_text("contact:\n  name: A\n")
    monkeypatch.setenv("JOB_INBOX_DIR", str(inbox))
    monkeypatch.setenv("JOB_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("JOB_MASTER", str(master))
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(job_ingest, "process_one", boom)

    rc = job_ingest.main()
    assert rc == 0
    assert not (inbox / "pending" / "e1.json").exists()
    rec = json.loads((inbox / "processed" / "e1.json").read_text())
    assert rec["status"] == "error"
    assert "kaboom" in rec["message"]


def test_main_records_and_removes_malformed_pending(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    (inbox / "pending").mkdir(parents=True)
    (inbox / "pending" / "bad.json").write_text(json.dumps({"id": "bad", "note": ""}))
    master = tmp_path / "master_resume.yaml"
    master.write_text("contact:\n  name: A\n")
    monkeypatch.setenv("JOB_INBOX_DIR", str(inbox))
    monkeypatch.setenv("JOB_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("JOB_MASTER", str(master))
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    rc = job_ingest.main()
    assert rc == 0
    assert not (inbox / "pending" / "bad.json").exists()   # cleared, not left forever
    rec = json.loads((inbox / "processed" / "bad.json").read_text())
    assert rec["status"] == "error"


def test_process_one_skips_already_advanced_job(db, monkeypatch):
    monkeypatch.setattr(job_ingest, "fetch_posting_meta", lambda parsed: {
        "title": "Staff SRE", "location": "Remote", "description": "JD"})
    job = {"ats": "greenhouse", "company": "acme", "id": "1", "title": "Staff SRE",
           "location": "Remote", "url": "https://boards.greenhouse.io/acme/jobs/1"}
    db.upsert_job(job)
    uid = make_job_uid("greenhouse", "acme", "1")
    db.set_state(uid, "queued"); db.set_state(uid, "drafted"); db.set_state(uid, "ready")

    gen = []
    rec = job_ingest.process_one(
        {"id": "e9", "url": "https://boards.greenhouse.io/acme/jobs/1", "note": "", "submitted_at": "t"},
        db, master={}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: pytest.fail("verdict must not run for an advanced job"),
        generate_fn=lambda *a, **k: gen.append(1),
        discord_fn=lambda *a, **k: True)
    assert rec["status"] == "scored"
    assert rec["state"] == "ready"
    assert gen == []
    assert db.get(uid)["state"] == "ready"


def test_process_one_already_handled_ping_uses_canonical_url(db, monkeypatch):
    # A LinkedIn link that resolves (chain-scrapes) to a greenhouse posting
    # already past 'queued' should ping with the canonical greenhouse URL,
    # not the raw submitted LinkedIn link.
    import job_fetch
    canonical = "https://boards.greenhouse.io/acme/jobs/777"
    submitted = "https://www.linkedin.com/jobs/view/123/"
    monkeypatch.setattr(job_fetch, "resolve_url", lambda u: {
        "ats": "greenhouse", "company": "acme", "ext_id": "777",
        "title": "Staff SRE", "location": "Remote", "description": "JD",
        "url": canonical, "posted_at": "", "date_source": ""})
    db.upsert_job({"ats": "greenhouse", "company": "acme", "id": "777",
                   "title": "Staff SRE", "location": "Remote", "url": canonical})
    uid = make_job_uid("greenhouse", "acme", "777")
    db.set_state(uid, "queued"); db.set_state(uid, "drafted")

    pings = []
    rec = job_ingest.process_one(
        {"id": "li9", "url": submitted, "note": "", "submitted_at": "t"},
        db, master={}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: pytest.fail("verdict must not run"),
        generate_fn=lambda *a, **k: pytest.fail("generate must not run"),
        discord_fn=lambda url, text, **kw: pings.append(text) or True)
    assert rec["status"] == "scored"
    assert rec["state"] == "drafted"
    assert any(canonical in p for p in pings)
    assert not any(submitted in p for p in pings)


def test_process_one_fetch_exception_is_isolated(db, monkeypatch):
    def boom(parsed):
        raise RuntimeError("network down")
    monkeypatch.setattr(job_ingest, "fetch_posting_meta", boom)
    pings = []
    rec = job_ingest.process_one(
        {"id": "e7", "url": "https://boards.greenhouse.io/acme/jobs/1", "note": "", "submitted_at": "t"},
        db, master={}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: pytest.fail("verdict must not run"),
        generate_fn=lambda *a, **k: pytest.fail("generate must not run"),
        discord_fn=lambda url, text, **kw: pings.append(text) or True)
    assert rec["status"] == "fetch_failed"
    assert "network down" in rec["message"]
    assert any("by hand" in p for p in pings)


def test_process_one_empty_jd_is_fetch_failed(db, monkeypatch):
    monkeypatch.setattr(job_ingest, "fetch_posting_meta", lambda parsed: {
        "title": "X", "location": "Y", "description": "   "})
    rec = job_ingest.process_one(
        {"id": "e8", "url": "https://boards.greenhouse.io/acme/jobs/1", "note": "", "submitted_at": "t"},
        db, master={}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: pytest.fail("verdict must not run"),
        generate_fn=lambda *a, **k: pytest.fail("generate must not run"),
        discord_fn=lambda *a, **k: True)
    assert rec["status"] == "fetch_failed"


def test_parse_ashby_url():
    r = job_ingest.parse_posting_url("https://jobs.ashbyhq.com/sentry/abc-123-uuid")
    assert r == {"ats": "ashby", "company": "sentry", "ext_id": "abc-123-uuid"}


def test_parse_smartrecruiters_url():
    r = job_ingest.parse_posting_url(
        "https://jobs.smartrecruiters.com/Canva/744000-senior-engineer")
    assert r == {"ats": "smartrecruiters", "company": "Canva", "ext_id": "744000"}


def test_fetch_posting_meta_ashby():
    payload = {"jobs": [
        {"id": "x1", "title": "Wrong one", "descriptionPlain": "no"},
        {"id": "abc", "title": "Staff Eng", "location": "Remote",
         "descriptionPlain": "Build stuff"},
    ]}
    with mock.patch.object(job_monitor.SESSION, "get", return_value=_resp(payload)):
        meta = job_ingest.fetch_posting_meta({"ats": "ashby", "company": "sentry", "ext_id": "abc"})
    assert meta["title"] == "Staff Eng"
    assert meta["location"] == "Remote"
    assert "Build stuff" in meta["description"]


def test_fetch_posting_meta_ashby_not_found_returns_empty():
    with mock.patch.object(job_monitor.SESSION, "get", return_value=_resp({"jobs": []})):
        meta = job_ingest.fetch_posting_meta({"ats": "ashby", "company": "x", "ext_id": "nope"})
    assert meta["description"] == ""


def test_fetch_posting_meta_smartrecruiters():
    payload = {"name": "Product Designer",
               "location": {"city": "Sydney", "country": "AU", "remote": True},
               "jobAd": {"sections": {
                   "jobDescription": {"text": "<p>Design things</p>"},
                   "qualifications": {"text": "<p>5y exp</p>"}}}}
    with mock.patch.object(job_monitor.SESSION, "get", return_value=_resp(payload)):
        meta = job_ingest.fetch_posting_meta(
            {"ats": "smartrecruiters", "company": "Canva", "ext_id": "744"})
    assert meta["title"] == "Product Designer"
    assert "Sydney" in meta["location"] and "remote" in meta["location"]
    assert "Design things" in meta["description"]
    assert "5y exp" in meta["description"]
    assert "<p>" not in meta["description"]


def test_process_one_paste_jd_for_unsupported_url_drafts(db):
    gen = []
    def fake_generate(db_, row, **kw):
        gen.append(row["ats"])
        return {"version": 1, "files": {"resume_docx": "/pkg/r.docx"}}
    rec = job_ingest.process_one(
        {"id": "m1", "url": "https://apply.omnicell.com/careers/job/123",
         "jd": "We need a platform engineer. Kubernetes, Terraform.", "note": "",
         "submitted_at": "t", "company": "Omnicell", "title": "Platform Engineer"},
        db, master={}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: {"llm_fit_score": 96, "llm_rationale": "great", "llm_coding_bar": "light"},
        generate_fn=fake_generate,
        gate_fn=lambda *a, **k: {"decision": gate.PROCEED, "report_path": "/tmp/r.md"},
        discord_fn=lambda *a, **k: True)
    assert rec["status"] == "drafted"
    assert rec["state"] == "drafted"
    assert rec["company"] == "Omnicell"
    assert rec["title"] == "Platform Engineer"
    assert gen == ["manual"]
    uid = job_ingest.make_job_uid(
        "manual", "Omnicell",
        job_ingest._manual_ext_id("https://apply.omnicell.com/careers/job/123"))
    assert db.get(uid)["state"] == "drafted"


def test_process_one_paste_jd_derives_company_from_host(db):
    rec = job_ingest.process_one(
        {"id": "m2", "url": "https://apply.omnicell.com/careers/job/123",
         "jd": "JD text here", "note": "", "submitted_at": "t"},
        db, master={}, api_key="k", webhook=None,
        verdict_fn=lambda *a, **k: {"llm_fit_score": 20, "llm_rationale": "no", "llm_coding_bar": ""},
        generate_fn=lambda *a, **k: pytest.fail("generate must not run below threshold"),
        discord_fn=lambda *a, **k: True)
    assert rec["company"] == "omnicell"
    assert rec["title"] == "Manual submission"
    assert rec["status"] == "scored"
    assert rec["state"] == "skipped"


def test_process_one_unsupported_url_no_jd_still_fetch_failed(db, monkeypatch):
    def no_resolve(url):
        raise job_fetch.FetchError("stubbed: unresolvable")
    monkeypatch.setattr(job_fetch, "resolve_url", no_resolve)
    rec = job_ingest.process_one(
        {"id": "m4", "url": "https://apply.omnicell.com/careers/job/123",
         "note": "", "submitted_at": "t"},
        db, master={}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: pytest.fail("verdict must not run"),
        generate_fn=lambda *a, **k: pytest.fail("generate must not run"),
        discord_fn=lambda *a, **k: True)
    assert rec["status"] == "fetch_failed"
    assert "Paste the JD" in rec["message"]


def test_process_one_supported_ats_fetch_fail_uses_pasted_jd(db, monkeypatch):
    def boom(parsed):
        raise RuntimeError("api down")
    monkeypatch.setattr(job_ingest, "fetch_posting_meta", boom)
    gen = []
    rec = job_ingest.process_one(
        {"id": "m3", "url": "https://boards.greenhouse.io/acme/jobs/1",
         "jd": "Fallback JD body", "note": "", "submitted_at": "t"},
        db, master={}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: {"llm_fit_score": 92, "llm_rationale": "y", "llm_coding_bar": ""},
        generate_fn=lambda db_, row, **kw: gen.append(1) or {"version": 1, "files": {}},
        gate_fn=lambda *a, **k: {"decision": gate.PROCEED, "report_path": "/tmp/r.md"},
        discord_fn=lambda *a, **k: True)
    assert rec["status"] == "drafted"
    assert rec["uid"] == job_ingest.make_job_uid("greenhouse", "acme", "1")
    assert gen == [1]


def test_process_one_drafted_resubmit_does_not_regenerate(db, monkeypatch):
    # A drafted job re-submitted must NOT re-fetch, re-score, or re-generate.
    job = {"ats": "greenhouse", "company": "acme", "id": "1", "title": "Staff SRE",
           "location": "Remote", "url": "https://boards.greenhouse.io/acme/jobs/1"}
    db.upsert_job(job)
    uid = make_job_uid("greenhouse", "acme", "1")
    db.set_state(uid, "queued"); db.set_state(uid, "drafted")

    gen, fetched = [], []
    monkeypatch.setattr(job_ingest, "fetch_posting_meta",
                        lambda parsed: fetched.append(1) or {"title": "x", "location": "y", "description": "JD"})
    rec = job_ingest.process_one(
        {"id": "e10", "url": "https://boards.greenhouse.io/acme/jobs/1", "note": "", "submitted_at": "t"},
        db, master={}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: pytest.fail("verdict must not run for a drafted job"),
        generate_fn=lambda *a, **k: gen.append(1),
        discord_fn=lambda *a, **k: True)
    assert rec["status"] == "scored"
    assert rec["state"] == "drafted"
    assert gen == []
    assert fetched == []                            # guard is pre-fetch, so no network call
    assert db.get(uid)["state"] == "drafted"


def test_process_one_linkedin_url_resolves_and_drafts(db, monkeypatch):
    import job_fetch
    monkeypatch.setattr(job_fetch, "resolve_url", lambda url: {
        "ats": "greenhouse", "company": "acme", "ext_id": "777",
        "title": "Senior SRE", "location": "Raleigh, NC",
        "description": "FULL JD",
        "url": "https://boards.greenhouse.io/acme/jobs/777",
        "posted_at": "2026-07-04", "date_source": "jsonld"})
    rec = job_ingest.process_one(
        {"id": "li1", "url": "https://www.linkedin.com/jobs/view/123/",
         "note": "", "submitted_at": "t"},
        db, master={}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: {"llm_fit_score": 95, "llm_rationale": "r",
                                    "llm_coding_bar": "light"},
        generate_fn=lambda *a, **k: {"version": 1, "files": {}},
        gate_fn=lambda *a, **k: {"decision": gate.PROCEED, "report_path": "/tmp/r.md"},
        discord_fn=lambda *a, **k: True)
    assert rec["status"] == "drafted"
    assert rec["uid"] == make_job_uid("greenhouse", "acme", "777")
    row = db.get(rec["uid"])
    assert row["description"] == "FULL JD"
    assert row["posted_at"] == "2026-07-04"
    assert row["date_source"] == "jsonld"


def test_process_one_web_resolve_failure_keeps_paste_hint(db, monkeypatch):
    import job_fetch
    def no_resolve(url):
        raise job_fetch.FetchError("no JobPosting metadata on https://x")
    monkeypatch.setattr(job_fetch, "resolve_url", no_resolve)
    pings = []
    rec = job_ingest.process_one(
        {"id": "w1", "url": "https://weird.example.com/job/1",
         "note": "", "submitted_at": "t"},
        db, master={}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: pytest.fail("verdict must not run"),
        generate_fn=lambda *a, **k: pytest.fail("generate must not run"),
        discord_fn=lambda url, text, **kw: pings.append(text) or True)
    assert rec["status"] == "fetch_failed"
    assert "no JobPosting metadata" in rec["message"]


def test_process_one_pasted_jd_skips_web_resolution(db, monkeypatch):
    import job_fetch
    monkeypatch.setattr(job_fetch, "resolve_url",
                        lambda url: pytest.fail("must not fetch when JD pasted"))
    rec = job_ingest.process_one(
        {"id": "m9", "url": "https://weird.example.com/job/2", "note": "",
         "submitted_at": "t", "jd": "PASTED JD", "title": "Staff Eng"},
        db, master={}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: {"llm_fit_score": 50, "llm_rationale": "r",
                                    "llm_coding_bar": "n/a"},
        generate_fn=lambda *a, **k: pytest.fail("no draft at 50"),
        discord_fn=lambda *a, **k: True)
    assert rec["status"] == "scored"
    assert rec["uid"].startswith("manual:")


def test_process_one_gate_blocked_does_not_draft(db, monkeypatch):
    """A high fit score alone must not draft: the ingest path is an artifact
    path, so it must be gated exactly like the CLI will be. A blocked gate
    must withhold generate_fn and say why, not draft anyway."""
    monkeypatch.setattr(job_ingest, "fetch_posting_meta", lambda parsed: {
        "title": "Staff SRE", "location": "Remote", "description": "JD BODY"})
    gen_called = []
    pings = []
    rec = job_ingest.process_one(
        _entry(), db, master={"contact": {"name": "A"}}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: {"llm_fit_score": 95, "llm_rationale": "great fit",
                                    "llm_coding_bar": "light"},
        generate_fn=lambda *a, **k: gen_called.append(1),
        gate_fn=lambda *a, **k: {"decision": gate.DO_NOT_APPLY,
                                 "report_path": "/tmp/fit-report.md"},
        discord_fn=lambda url, text, **kw: pings.append(text) or True)
    assert gen_called == []                         # gate blocked it; never drafted
    assert rec["status"] != "drafted"
    assert rec["state"] != "drafted"
    assert db.get(rec["uid"])["state"] != "drafted"
    assert any("gate" in p.lower() and "fit-report.md" in p for p in pings)


def test_process_one_gate_proceed_drafts(db, monkeypatch):
    """A high fit score plus a PROCEED gate is what actually clears to draft."""
    monkeypatch.setattr(job_ingest, "fetch_posting_meta", lambda parsed: {
        "title": "Staff SRE", "location": "Remote", "description": "JD BODY"})
    rec = job_ingest.process_one(
        _entry(), db, master={"contact": {"name": "A"}}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: {"llm_fit_score": 95, "llm_rationale": "great fit",
                                    "llm_coding_bar": "light"},
        generate_fn=lambda db_, row, **kw: {"version": 1,
                                            "files": {"resume_docx": "/pkg/r.docx"}},
        gate_fn=lambda *a, **k: {"decision": gate.PROCEED, "report_path": "/tmp/r.md"},
        discord_fn=lambda *a, **k: True)
    assert rec["status"] == "drafted"
    assert rec["state"] == "drafted"
    assert db.get(rec["uid"])["state"] == "drafted"


def test_process_one_default_gate_fn_is_real_gate_run_gate(db, monkeypatch):
    """No gate_fn override supplied: production default must be the real
    gate.run_gate, not a bypass. Patch the gate module's own attribute (not
    job_ingest's) so this proves the default resolves through the real thing."""
    monkeypatch.setattr(job_ingest, "fetch_posting_meta", lambda parsed: {
        "title": "Staff SRE", "location": "Remote", "description": "JD BODY"})
    calls = []

    def fake_run_gate(db_, row, master, **kw):
        calls.append(kw)
        return {"decision": gate.PROCEED, "report_path": "/tmp/r.md"}

    monkeypatch.setattr(gate, "run_gate", fake_run_gate)
    rec = job_ingest.process_one(
        _entry(), db, master={"contact": {"name": "A"}}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: {"llm_fit_score": 95, "llm_rationale": "great fit",
                                    "llm_coding_bar": "light"},
        generate_fn=lambda db_, row, **kw: {"version": 1, "files": {}},
        discord_fn=lambda *a, **k: True)
    assert calls, "default gate_fn must call the real gate.run_gate"
    assert rec["status"] == "drafted"


# --- Workday URL ingest ---------------------------------------------------
#
# A pasted Workday posting used to fall through every matcher to the generic
# JSON-LD path, which stored ats='manual' and a location scraped out of the
# page's schema.org blob. On one live posting that blob
# yielded "<Employer>, United States of America" while the ATS itself
# published "Remote - United States", so the gate's location overlay returned
# NOT_REMOTE and blocked a genuinely remote role. The cxs endpoint carries the
# right location, so parse the URL instead of scraping the page.

def test_parse_workday_url():
    r = job_ingest.parse_posting_url(
        "https://pennmutual.wd1.myworkdayjobs.com/_penn-careers"
        "/job/Remote---United-States/Sr-Manager_R-100661")
    assert r == {
        "ats": "workday",
        "company": "pennmutual.wd1.myworkdayjobs.com/_penn-careers",
        "ext_id": "/job/Remote---United-States/Sr-Manager_R-100661",
    }


def test_parse_workday_url_strips_locale_segment():
    """Workday serves the same posting with and without an /en-US/ prefix
    (both return 200 live), but the cxs endpoint takes neither, so the locale
    has to come off or the two forms produce two different uids."""
    plain = job_ingest.parse_posting_url(
        "https://acme.wd5.myworkdayjobs.com/careers/job/Boston/Eng_R-1")
    localed = job_ingest.parse_posting_url(
        "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Boston/Eng_R-1")
    assert localed == plain
    assert plain["ext_id"] == "/job/Boston/Eng_R-1"


def test_parse_workday_url_ignores_query_string():
    r = job_ingest.parse_posting_url(
        "https://acme.wd1.myworkdayjobs.com/careers/job/Boston/Eng_R-1?source=LinkedIn")
    assert r["ext_id"] == "/job/Boston/Eng_R-1"


def test_parse_workday_url_matches_what_the_scanner_stores():
    """The uid layer of dedup only works if a pasted URL and a scanner hit
    agree on company and ext_id. job_monitor.fetch_workday stores
    company='{host}/{site}' and ext_id=externalPath; parse must produce both."""
    host, site = "acme.wd1.myworkdayjobs.com", "careers"
    external_path = "/job/Boston/Eng_R-1"
    r = job_ingest.parse_posting_url(f"https://{host}/{site}{external_path}")
    assert r["company"] == f"{host}/{site}"
    assert r["ext_id"] == external_path


def test_parse_workday_non_posting_paths_return_none():
    # The board root and a search page are not postings.
    assert job_ingest.parse_posting_url(
        "https://acme.wd1.myworkdayjobs.com/careers") is None
    assert job_ingest.parse_posting_url(
        "https://acme.wd1.myworkdayjobs.com/careers/search") is None
    # A /job/ segment with nothing after it is not a posting either.
    assert job_ingest.parse_posting_url(
        "https://acme.wd1.myworkdayjobs.com/careers/job") is None


def test_parse_workday_spoofed_host_returns_none():
    assert job_ingest.parse_posting_url(
        "https://acme.wd1.myworkdayjobs.com.attacker.example/careers/job/B/Eng_R-1") is None
    assert job_ingest.parse_posting_url(
        "https://notmyworkdayjobs.com/careers/job/B/Eng_R-1") is None


def test_parse_workday_url_builds_the_live_cxs_endpoint():
    """The regression guard: parse -> posting_endpoint must produce the URL
    that actually answered 200 for R-100661. posting_endpoint is the single
    definition shared with fetch_description and liveness.check, so if the
    parsed shape is wrong the gate 404s and returns ERROR, which blocks
    drafting exactly like DO_NOT_APPLY."""
    import job_generate
    parsed = job_ingest.parse_posting_url(
        "https://pennmutual.wd1.myworkdayjobs.com/_penn-careers"
        "/job/Remote---United-States/Sr-Manager_R-100661")
    assert job_generate.posting_endpoint(parsed) == (
        "https://pennmutual.wd1.myworkdayjobs.com/wday/cxs/pennmutual/_penn-careers"
        "/job/Remote---United-States/Sr-Manager_R-100661")


def test_fetch_posting_meta_workday():
    payload = {"jobPostingInfo": {
        "title": "Sr. Manager, Site Reliability Engineering (DevOps)",
        "location": "Remote - United States",
        "jobDescription": "<p>Lead <b>DevOps</b> engineers</p>",
    }}
    with mock.patch.object(job_monitor.SESSION, "get", return_value=_resp(payload)):
        meta = job_ingest.fetch_posting_meta(
            {"ats": "workday",
             "company": "pennmutual.wd1.myworkdayjobs.com/_penn-careers",
             "ext_id": "/job/Remote---United-States/Sr-Manager_R-100661"})
    assert meta["title"] == "Sr. Manager, Site Reliability Engineering (DevOps)"
    # The whole point of the fix: the real location, not the JSON-LD guess.
    assert meta["location"] == "Remote - United States"
    assert "Lead DevOps engineers" in meta["description"]
    assert "<p>" not in meta["description"]


def test_fetch_posting_meta_workday_empty_payload_is_blank_not_crash():
    with mock.patch.object(job_monitor.SESSION, "get", return_value=_resp({})):
        meta = job_ingest.fetch_posting_meta(
            {"ats": "workday", "company": "acme.wd1.myworkdayjobs.com/careers",
             "ext_id": "/job/B/Eng_R-1"})
    assert meta == {"title": "", "location": "", "description": "",
                    "posted_at": "", "date_source": ""}


def test_parse_workday_two_letter_site_slug_is_not_eaten_as_a_locale():
    """"/us/job/..." is a site named "us", not a locale. The parser reads the
    path as-is first and only falls back to stripping a leading locale."""
    r = job_ingest.parse_posting_url(
        "https://acme.wd1.myworkdayjobs.com/us/job/Boston/Eng_R-1")
    assert r["company"] == "acme.wd1.myworkdayjobs.com/us"
    assert r["ext_id"] == "/job/Boston/Eng_R-1"


def test_fetch_posting_meta_workday_reports_the_posting_date():
    """Same convention job_monitor.fetch_workday uses for a scanner hit:
    postedOn is relative text, so the date is approximate and the source
    string carries the ~ that says so."""
    payload = {"jobPostingInfo": {
        "title": "Sr. Manager, SRE", "location": "Remote - United States",
        "jobDescription": "<p>d</p>", "postedOn": "Posted 3 Days Ago",
    }}
    with mock.patch.object(job_monitor.SESSION, "get", return_value=_resp(payload)):
        meta = job_ingest.fetch_posting_meta(
            {"ats": "workday", "company": "acme.wd1.myworkdayjobs.com/careers",
             "ext_id": "/job/B/Eng_R-1"})
    from datetime import datetime, timedelta, timezone
    expected = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
    assert meta["posted_at"] == expected
    assert meta["date_source"] == "workday:postedOn~"


def test_fetch_posting_meta_workday_undatable_posting_is_blank_not_wrong():
    """An unparseable postedOn must leave the date empty. Freshness KEEPS
    undated rows and labels them "age unknown"; inventing a date would quietly
    age a live posting out of the default view instead."""
    payload = {"jobPostingInfo": {"title": "t", "location": "l",
                                  "jobDescription": "d", "postedOn": "whenever"}}
    with mock.patch.object(job_monitor.SESSION, "get", return_value=_resp(payload)):
        meta = job_ingest.fetch_posting_meta(
            {"ats": "workday", "company": "acme.wd1.myworkdayjobs.com/careers",
             "ext_id": "/job/B/Eng_R-1"})
    assert meta["posted_at"] == ""
    assert meta["date_source"] == ""


def test_process_one_parsed_url_keeps_the_posting_date(db, monkeypatch):
    """Codex P2 on PR #109. The parsed branch hardcoded posted_at="" and
    date_source to the ingest label, so a Workday URL submitted through the lead
    inbox landed undated ("age unknown") while the SAME url through
    `jh fetch` landed dated. The two ingest paths have to agree."""
    monkeypatch.setattr(job_ingest, "fetch_posting_meta", lambda parsed: {
        "title": "Sr. Manager, SRE", "location": "Remote - United States",
        "description": "JD BODY", "posted_at": "2026-09-01",
        "date_source": "workday:postedOn~"})
    entry = {"id": "e1", "note": "", "submitted_at": "t0",
             "url": "https://globex.wd1.myworkdayjobs.com/_globex-careers"
                    "/job/Remote---United-States/Sr-Manager_R-100661"}
    rec = job_ingest.process_one(
        entry, db, master={"contact": {"name": "A"}}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: {"llm_fit_score": 10, "llm_rationale": "no",
                                    "llm_coding_bar": "light"},
        generate_fn=lambda *a, **k: None,
        gate_fn=lambda *a, **k: {"decision": gate.PROCEED, "report_path": "/tmp/r.md"},
        discord_fn=lambda url, text, **kw: True)
    row = db.get(rec["uid"])
    assert row["posted_at"] == "2026-09-01"
    assert row["date_source"] == "workday:postedOn~"


def test_process_one_parsed_url_without_a_date_keeps_the_ingest_label(db, monkeypatch):
    """The other four ATS metadata fetchers report no date. They must keep the
    existing ingest provenance label rather than an empty one."""
    monkeypatch.setattr(job_ingest, "fetch_posting_meta", lambda parsed: {
        "title": "Staff SRE", "location": "Remote", "description": "JD BODY"})
    rec = job_ingest.process_one(
        _entry(), db, master={"contact": {"name": "A"}}, api_key="k", webhook="http://hook",
        verdict_fn=lambda *a, **k: {"llm_fit_score": 10, "llm_rationale": "no",
                                    "llm_coding_bar": "light"},
        generate_fn=lambda *a, **k: None,
        gate_fn=lambda *a, **k: {"decision": gate.PROCEED, "report_path": "/tmp/r.md"},
        discord_fn=lambda url, text, **kw: True)
    row = db.get(rec["uid"])
    assert row["posted_at"] == ""
    assert row["date_source"] == job_ingest.SOURCE
