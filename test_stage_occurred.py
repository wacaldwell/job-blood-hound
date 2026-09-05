"""`jh stage` must be able to say WHEN a round happened, not just that it did.

interview_updated is documented as "when the marker last moved" and The Loop
measures its quiet clock from it. set_stage stamped now() unconditionally, so
recording a round after the fact reset the clock to the moment of recording.
Live case (2026-08-20): one loop's technical round was held 2026-08-13 and
recorded a
week later, and the board read 0 days quiet and green when it should have read
7 days and amber. The page exists to surface silence, so a clock that resets
when you take notes defeats it.
"""
from datetime import datetime, timedelta, timezone

import pytest

import jobdb
from jobdb import JobDB


def _job(tmp_path, ident="acme"):
    db = JobDB(tmp_path / "t.db")
    db.upsert_job({
        "id": ident, "ats": "greenhouse", "company": ident,
        "title": "DevOps Manager", "location": "Remote",
        "url": f"https://example.com/{ident}",
    })
    uid = jobdb.make_job_uid("greenhouse", ident, ident)
    for s in ("queued", "drafted", "ready", "applied", "interviewing"):
        db.set_state(uid, s)
    return db, uid


def test_occurred_sets_the_clock_to_when_the_round_happened(tmp_path):
    tmp_db, uid = _job(tmp_path)
    tmp_db.set_stage(uid, at=2, occurred="2026-08-13")
    row = tmp_db.get(uid)
    assert row["interview_updated"].startswith("2026-08-13")


def test_omitting_occurred_still_stamps_now(tmp_path):
    tmp_db, uid = _job(tmp_path)
    tmp_db.set_stage(uid, at=2)
    row = tmp_db.get(uid)
    assert row["interview_updated"].startswith(jobdb.now_iso()[:10])


def test_a_future_date_is_refused(tmp_path):
    """A round cannot have happened tomorrow, and accepting one would make the
    quiet clock negative on a page whose whole job is measuring elapsed time."""
    tmp_db, uid = _job(tmp_path)
    with pytest.raises(ValueError, match="future"):
        tmp_db.set_stage(uid, at=2, occurred="2099-01-01")


def test_a_malformed_date_is_refused(tmp_path):
    tmp_db, uid = _job(tmp_path)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        tmp_db.set_stage(uid, at=2, occurred="last tuesday")


def test_occurred_works_for_the_decision_terminal_too(tmp_path):
    """The live case: all rounds done, decision pending since the final
    round was held, and that date is the one the clock must run from."""
    tmp_db, uid = _job(tmp_path)
    tmp_db.set_stage(uid, decision=True, occurred="2026-08-13",
                     next_note="awaiting offer or rejection")
    row = tmp_db.get(uid)
    assert row["interview_decision"] == 1
    assert row["interview_updated"].startswith("2026-08-13")
    assert row["interview_next"] == "awaiting offer or rejection"


def test_the_audit_row_records_the_backdate(tmp_path):
    """A backdated clock must be visible in the trail, or it looks like the
    marker moved on a day nothing happened."""
    tmp_db, uid = _job(tmp_path)
    tmp_db.set_stage(uid, at=2, occurred="2026-08-13")
    notes = " ".join((r["note"] or "") for r in tmp_db.history(uid))
    assert "2026-08-13" in notes


# --- nothing is written until every argument has been validated ------------
#
# `jh stage` used to transition applied -> interviewing FIRST and only then
# call set_stage, which is where `--on` and the round range are checked. A
# rejected command therefore exited non-zero having already committed an
# audited state change with no marker to show for it. The marker write goes
# first now, and the lifecycle transition second.

def _applied(db, ident="acme"):
    db.upsert_job({"id": ident, "ats": "greenhouse", "company": ident,
                   "title": "Senior SRE", "location": "Remote", "url": "http://x"})
    uid = jobdb.make_job_uid("greenhouse", ident, ident)
    for s in ("queued", "drafted", "ready", "applied"):
        db.set_state(uid, s)
    return uid


def _stage(db, ident, round_, on=None):
    import argparse
    import job_cli
    return job_cli.cmd_stage(
        db, argparse.Namespace(ident=ident, round=round_, next=None, on=on))


def test_a_malformed_date_leaves_the_state_untouched(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _applied(db)
    with pytest.raises(SystemExit):
        _stage(db, "acme", "2", on="not-a-date")
    row = db.get(uid)
    assert row["state"] == "applied"
    assert row["interview_at"] is None
    assert not [h for h in db.history(uid) if h["to_state"] == "interviewing"]
    db.close()


def test_a_future_date_leaves_the_state_untouched(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _applied(db)
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    with pytest.raises(SystemExit):
        _stage(db, "acme", "2", on=tomorrow)
    assert db.get(uid)["state"] == "applied"
    db.close()


def test_a_round_outside_the_list_leaves_the_state_untouched(tmp_path):
    """Same defect, same fix: the range check also lives inside set_stage."""
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _applied(db)
    db.set_rounds(uid, ["recruiter", "technical"])
    with pytest.raises(SystemExit):
        _stage(db, "acme", "9")
    assert db.get(uid)["state"] == "applied"
    db.close()


def test_a_valid_stage_still_transitions_and_marks(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _applied(db)
    _stage(db, "acme", "2", on="2026-08-13")
    row = db.get(uid)
    assert row["state"] == "interviewing"
    assert row["interview_at"] == 2
    assert row["interview_updated"].startswith("2026-08-13")
    db.close()
