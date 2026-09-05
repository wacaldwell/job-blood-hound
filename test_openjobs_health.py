"""The wide net has to report its own health into the digest.

Deferred finding M2 from the second review round, and 2026-09-01 proved it:
the corpus `/embed` endpoint was down, so the stage correctly produced nothing
and exited 0, and the ONLY trace was a line in daily.log that nobody reads.
A run that found 15 leads and a run where the corpus was unreachable left the
same observable state. Fail-safe was right; fail-silent was not.
"""
import json

import fit
import job_cli
import jobdb
import openjobs


CFG = {"title_terms": ["engineer"], "exclude_terms": [], "location_terms": ["remote"]}


def cand(n, sim=0.9):
    return {"id": str(n), "title": f"Platform Engineer {n}", "location": "Remote",
            "url": f"https://example.com/{n}", "company": f"co{n}",
            "company_display": f"Co {n}", "ats": "greenhouse", "posted_at": "2026-08-01",
            "date_source": "openjobs:first_seen~", "description": "AWS.",
            "location_type": "remote", "source": "openjobs", "sim": sim}


# -- the stage records what happened ----------------------------------------

def test_a_successful_run_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_OPENJOBS_CACHE", str(tmp_path / "cache"))
    db = jobdb.JobDB(tmp_path / "jobs.db")
    job_cli.openjobs_and_ingest(db, CFG, top=5,
                                discover=lambda *a, **k: [cand(1), cand(2)])
    status = openjobs.read_status(cache=tmp_path / "cache")
    assert status["added"] == 2
    assert status["error"] is None
    assert status["at"]


def test_a_failed_run_records_the_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_OPENJOBS_CACHE", str(tmp_path / "cache"))
    db = jobdb.JobDB(tmp_path / "jobs.db")

    def boom(*a, **k):
        raise RuntimeError("429 rate limited")

    job_cli.openjobs_and_ingest(db, CFG, top=5, discover=boom)
    status = openjobs.read_status(cache=tmp_path / "cache")
    assert status["added"] == 0
    assert "429" in status["error"]


def test_recording_the_status_never_costs_the_run_its_leads(tmp_path, monkeypatch):
    """Housekeeping must not be able to lose a lead."""
    monkeypatch.setenv("JOB_OPENJOBS_CACHE", "/proc/nonexistent/cannot-write")
    db = jobdb.JobDB(tmp_path / "jobs.db")
    r = job_cli.openjobs_and_ingest(db, CFG, top=5,
                                    discover=lambda *a, **k: [cand(1)])
    assert r["added"] == 1


# -- the digest says so -----------------------------------------------------

def test_the_digest_reports_a_successful_wide_net():
    line = fit.wide_net_line({"added": 12, "found": 250, "error": None,
                              "at": "2026-09-01T10:31:00+00:00"})
    assert "12" in line
    assert "wide net" in line.lower()


def test_the_digest_reports_a_failure_loudly_enough_to_notice():
    line = fit.wide_net_line({"added": 0, "found": 0,
                              "error": "CorpusError: 429 Too Many Requests",
                              "at": "2026-09-01T10:31:00+00:00"})
    assert "429" in line
    assert "unavailable" in line.lower()


def test_a_quiet_but_healthy_run_still_reports():
    """Zero new leads is a real answer and must not read as a failure."""
    line = fit.wide_net_line({"added": 0, "found": 250, "error": None,
                              "at": "2026-09-01T10:31:00+00:00"})
    assert "unavailable" not in line.lower()
    assert "0" in line


def test_no_status_at_all_produces_no_line():
    """Before the first run, and on any host where the stage is switched off,
    the digest must stay exactly as it was."""
    assert fit.wide_net_line(None) == ""
    assert fit.wide_net_line({}) == ""


def test_the_line_reaches_the_rendered_digest():
    text, _ = fit.build_digest_sections(
        [], [], {"discovered": 3},
        wide_net={"added": 0, "found": 0, "error": "CorpusError: 503", "at": "x"})
    assert "503" in text


def test_the_digest_is_unchanged_when_the_wide_net_is_not_reporting():
    without, _ = fit.build_digest_sections([], [], {"discovered": 3})
    none_status, _ = fit.build_digest_sections([], [], {"discovered": 3},
                                               wide_net=None)
    assert without == none_status


# -- the failure has to survive discover()'s own fail-safe ------------------

def test_the_real_fail_safe_path_is_recorded_as_a_failure(tmp_path, monkeypatch):
    """The finding that mattered, and it defeated the first version of this.

    `discover()` catches CorpusError and returns [], which is correct: the
    stage must fail safe. But that meant openjobs_and_ingest took its SUCCESS
    path with error=None, so the exact 503 this feature was built for rendered
    as "0 new from 0 candidates", indistinguishable from a quiet day. The
    original failure test only injected a function that raised PAST discover(),
    which is the one path production never takes.
    """
    monkeypatch.setenv("JOB_OPENJOBS_CACHE", str(tmp_path / "cache"))
    db = jobdb.JobDB(tmp_path / "jobs.db")

    def embed_503(text):
        raise openjobs.CorpusError("embed failed: HTTPError: 503 Server Error")

    real = openjobs.discover
    job_cli.openjobs_and_ingest(
        db, CFG, top=15,
        discover=lambda c, **k: real(c, embed=embed_503, jd_text="x", **k))

    status = openjobs.read_status(cache=tmp_path / "cache")
    assert status["error"], "a caught CorpusError was recorded as success"
    assert "503" in status["error"]
    assert "unavailable" in fit.wide_net_line(status).lower()


def test_a_missing_ideal_jd_is_also_recorded_as_a_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_OPENJOBS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(openjobs, "IDEAL_JD_PATH", tmp_path / "nope.md")
    db = jobdb.JobDB(tmp_path / "jobs.db")
    real = openjobs.discover
    job_cli.openjobs_and_ingest(db, CFG, top=15,
                                discover=lambda c, **k: real(c, **k))
    status = openjobs.read_status(cache=tmp_path / "cache")
    assert status["error"], "a missing ideal JD looked like a healthy empty run"


def test_a_genuinely_empty_but_healthy_run_is_not_called_a_failure(tmp_path, monkeypatch):
    """The other direction: zero candidates with a working corpus is a real
    answer, and must not cry wolf."""
    monkeypatch.setenv("JOB_OPENJOBS_CACHE", str(tmp_path / "cache"))
    db = jobdb.JobDB(tmp_path / "jobs.db")
    job_cli.openjobs_and_ingest(db, CFG, top=15, discover=lambda *a, **k: [])
    status = openjobs.read_status(cache=tmp_path / "cache")
    assert status["error"] is None
    assert "unavailable" not in fit.wide_net_line(status).lower()


# -- a stale status must not pass for a fresh one ---------------------------

def test_yesterdays_status_is_not_reported_as_todays(tmp_path):
    """If the stage times out (bin/daily.sh caps it) or is switched off with
    OPENJOBS_ENABLED=0, write_status never runs, and an unbounded read would
    keep reporting the last success in every future digest."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / openjobs.STATUS_FILE).write_text(json.dumps(
        {"added": 12, "found": 250, "error": None,
         "at": "2026-08-20T10:00:00+00:00"}))
    assert openjobs.read_status(cache=cache) is None


def test_a_status_from_this_mornings_run_is_still_reported(tmp_path):
    import time as _t
    cache = tmp_path / "cache"
    cache.mkdir()
    recent = _t.strftime("%Y-%m-%dT%H:%M:%S+00:00", _t.gmtime(_t.time() - 3600))
    (cache / openjobs.STATUS_FILE).write_text(json.dumps(
        {"added": 12, "found": 250, "error": None, "at": recent}))
    assert openjobs.read_status(cache=cache)["added"] == 12


def test_an_unparseable_timestamp_is_treated_as_stale(tmp_path):
    """Fail toward silence, not toward a stale claim."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / openjobs.STATUS_FILE).write_text(json.dumps(
        {"added": 12, "found": 250, "error": None, "at": "not a date"}))
    assert openjobs.read_status(cache=cache) is None
