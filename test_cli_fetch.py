import argparse
import pytest
import gate
import job_cli
import job_fetch
from jobdb import JobDB, make_job_uid


@pytest.fixture
def db(tmp_path):
    return JobDB(str(tmp_path / "jobs.db"))


def _resolved(**over):
    base = {"ats": "greenhouse", "company": "acme", "ext_id": "777",
            "title": "Senior SRE", "location": "Raleigh, NC",
            "description": "JD BODY",
            "url": "https://boards.greenhouse.io/acme/jobs/777",
            "posted_at": "2026-07-04", "date_source": "jsonld"}
    base.update(over)
    return base


def _args(url):
    return argparse.Namespace(url=url)


def test_cmd_fetch_ingests_and_queues(db, monkeypatch, capsys):
    monkeypatch.setattr(job_fetch, "resolve_url", lambda url: _resolved())
    # fetch runs the gate; a PROCEED stub keeps this a fetch/queue test, not a
    # gate test (the gate itself is covered in test_gate_cli.py and test_gate_run.py).
    monkeypatch.setattr(gate, "run_gate", lambda *a, **k: {
        "decision": gate.PROCEED, "title": {}, "report_path": "/tmp/r.md",
        "counts": {"known_hard_none": 0, "unresolved": 0}})
    job_cli.cmd_fetch(db, _args("https://www.linkedin.com/jobs/view/123/"))
    uid = make_job_uid("greenhouse", "acme", "777")
    row = db.get(uid)
    assert row["state"] == "queued"
    assert row["description"] == "JD BODY"
    assert row["posted_at"] == "2026-07-04"
    assert row["date_source"] == "jsonld"
    out = capsys.readouterr().out
    assert "Senior SRE" in out and "draft" in out


def test_cmd_fetch_existing_job_untouched(db, monkeypatch, capsys):
    monkeypatch.setattr(job_fetch, "resolve_url", lambda url: _resolved())
    db.upsert_job({"ats": "greenhouse", "company": "acme", "id": "777",
                   "title": "Senior SRE", "location": "", "url": "u"})
    uid = make_job_uid("greenhouse", "acme", "777")
    db.set_state(uid, "queued")
    db.set_state(uid, "drafted")
    job_cli.cmd_fetch(db, _args("https://boards.greenhouse.io/acme/jobs/777"))
    assert db.get(uid)["state"] == "drafted"           # not disturbed
    assert "already tracked" in capsys.readouterr().out


def test_cmd_fetch_error_exits_nonzero(db, monkeypatch, capsys):
    def boom(url):
        raise job_fetch.FetchError("no JobPosting metadata")
    monkeypatch.setattr(job_fetch, "resolve_url", boom)
    with pytest.raises(SystemExit) as e:
        job_cli.cmd_fetch(db, _args("https://example.com/x"))
    assert e.value.code == 1
    assert "no JobPosting metadata" in capsys.readouterr().out


def test_fetch_wired_into_parser():
    args = job_cli.build_parser().parse_args(
        ["fetch", "https://www.linkedin.com/jobs/view/1/"])
    assert args.cmd == "fetch"
    assert args.url == "https://www.linkedin.com/jobs/view/1/"
    assert job_cli.DISPATCH["fetch"] is job_cli.cmd_fetch
