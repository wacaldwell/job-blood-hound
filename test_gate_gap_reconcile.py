"""The gaps table must be a pure function of the current hard-NONE set,
reconciled on every persist (run_gate AND recompute/gate-rule).
"""
import json
import pytest
import gate
import jobdb
import job_cli

MASTER = {"works_as": [], "capabilities": [], "do_not_claim": []}


def _db(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"ats": "greenhouse", "company": "globex", "id": "1",
                   "title": "Senior Staff Operations Engineer", "location": "Remote"})
    return db, db.get(jobdb.make_job_uid("greenhouse", "globex", "1"))


def _gate_with_one_hard_none(db, row, quote="Proficient in Python or Go",
                             confidence="high"):
    def fake_call(system, user, api_key):
        return json.dumps({"requirements": [
            {"quote": quote, "topic": "python or go", "hard": True,
             "confidence": confidence, "verdict": "NONE", "evidence": "", "bridge": ""},
        ]})
    return gate.run_gate(db, row, MASTER, api_key="k", jd_text="jd", call=fake_call)


def test_ruling_a_hard_none_to_soft_closes_its_gap(tmp_path, monkeypatch):
    # Only a genuinely UNSURE (low confidence) classification is adjudicable
    # via gate-rule, so this fixture is low-confidence: it starts NEEDS_REVIEW,
    # not CONDITIONAL, until the human rules on it.
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))
    db, row = _db(tmp_path)
    out = _gate_with_one_hard_none(db, row, confidence="low")
    assert out["decision"] == gate.NEEDS_REVIEW
    gaps = db.gaps_for(row["uid"])
    assert len(gaps) == 1
    assert gaps[0]["status"] == "open"

    args = type("A", (), {"ident": row["slug"], "n": 1, "hard": False,
                          "soft": True, "note": "reads as soft to me"})()
    job_cli.cmd_gate_rule(db, args)

    ruled = db.get(row["uid"])
    assert ruled["gate_decision"] == gate.PROCEED
    # jh gaps no longer lists it as open.
    assert db.open_gaps() == []
    gaps_after = db.gaps_for(row["uid"])
    assert len(gaps_after) == 1
    assert gaps_after[0]["status"] == "closed"


def test_rerunning_the_gate_does_not_reopen_a_gap_closed_by_hand(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))
    db, row = _db(tmp_path)
    quote = "Proficient in Python or Go"
    _gate_with_one_hard_none(db, row, quote)
    gid = db.gaps_for(row["uid"])[0]["id"]
    db.close_gap(gid, "Studied Go, comfortable with it now.")  # done by hand

    # Re-run the gate; the requirement is STILL a hard NONE.
    _gate_with_one_hard_none(db, db.get(row["uid"]), quote)

    gaps = db.gaps_for(row["uid"])
    assert len(gaps) == 1, "must not add a second gap row for the same requirement"
    assert gaps[0]["status"] == "closed", "must not reopen a gap closed by hand"


def test_gap_for_a_still_hard_none_requirement_stays_open(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))
    db, row = _db(tmp_path)
    quote = "Proficient in Python or Go"
    _gate_with_one_hard_none(db, row, quote)

    # A plain recompute (no ruling change): the requirement is unchanged, so
    # the gap must remain open, not get closed as collateral damage.
    out = gate.recompute(db, db.get(row["uid"]))
    assert out["decision"] == gate.CONDITIONAL
    gaps = db.gaps_for(row["uid"])
    assert len(gaps) == 1
    assert gaps[0]["status"] == "open"


def test_ruling_soft_then_hard_again_reopens_the_gap_and_still_blocks(tmp_path, monkeypatch):
    """The round trip. A change of mind must not become a silent bypass.
    soft -> the gap auto-closes and the job PROCEEDs.
    a fresh gate run bringing it back hard -> the requirement is a hard NONE
    once more, so its gap must REOPEN and the job must be blocked again until
    it is planned.

    Note this is a fresh gate run, not a second gate-rule call on the same
    ruling: once a requirement has been ruled by a human it is no longer
    UNSURE, so gate-rule refuses to touch it again (see test_gate_no_bypass.py).
    A later, independent gate run is not bound by an earlier ruling."""
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))
    db, row = _db(tmp_path)
    quote = "Proficient in Python or Go"
    out = _gate_with_one_hard_none(db, row, quote, confidence="low")
    assert out["decision"] == gate.NEEDS_REVIEW

    args_soft = type("A", (), {"ident": row["slug"], "n": 1, "hard": False,
                                "soft": True, "note": "reads as soft to me"})()
    job_cli.cmd_gate_rule(db, args_soft)

    ruled = db.get(row["uid"])
    assert ruled["gate_decision"] == gate.PROCEED
    gap = db.gaps_for(row["uid"])[0]
    assert gap["status"] == "closed"
    assert gap["closed_reason"] == "reclassified"
    gate.require_pass(db, db.get(row["uid"]))  # PROCEED: does not raise

    # A fresh gate run (e.g. a re-scan of the JD) brings the requirement back
    # as a confident hard NONE, independent of the earlier ruling.
    _gate_with_one_hard_none(db, db.get(row["uid"]), quote, confidence="high")

    ruled2 = db.get(row["uid"])
    assert ruled2["gate_decision"] == gate.CONDITIONAL
    gap2 = db.gaps_for(row["uid"])[0]
    assert gap2["status"] == "open", "the gap must reopen: the human never planned it"
    assert gap2["id"] == gap["id"], "reopen the same gap row, not a new one"

    with pytest.raises(gate.GateBlocked):
        gate.require_pass(db, db.get(row["uid"]))


def test_a_gap_the_human_closed_is_never_reopened_by_a_re_run(tmp_path, monkeypatch):
    """A gap closed via `jh gap-close` means he did the work. Re-running the
    gate on a requirement that is STILL a hard NONE must not resurrect it."""
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))
    db, row = _db(tmp_path)
    quote = "Proficient in Python or Go"
    _gate_with_one_hard_none(db, row, quote)
    gid = db.gaps_for(row["uid"])[0]["id"]
    db.close_gap(gid, "Studied Go, comfortable with it now.")  # done by hand

    # Re-run the gate; the requirement is STILL a hard NONE.
    _gate_with_one_hard_none(db, db.get(row["uid"]), quote)

    gaps = db.gaps_for(row["uid"])
    assert len(gaps) == 1, "must not add a second gap row for the same requirement"
    assert gaps[0]["status"] == "closed", "must not reopen a gap closed by hand"
    assert gaps[0]["closed_reason"] == "planned"


def test_a_system_closed_gap_does_not_survive_a_re_run_when_still_hard_none(tmp_path, monkeypatch):
    """A gap the SYSTEM auto-closed (a ruling moved it off the hard-NONE set)
    must reopen on a fresh gate run, not just recompute, if the requirement
    comes back as a hard NONE, e.g. a re-scan of the job description."""
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))
    db, row = _db(tmp_path)
    quote = "Proficient in Python or Go"

    _gate_with_one_hard_none(db, row, quote)
    gid = db.gaps_for(row["uid"])[0]["id"]

    # Simulate a rescan where the requirement no longer reads as a hard NONE:
    # the system closes the gap.
    def fake_call_soft(system, user, api_key):
        return json.dumps({"requirements": [
            {"quote": quote, "topic": "python or go", "hard": False,
             "confidence": "high", "verdict": "NONE", "evidence": "", "bridge": ""},
        ]})
    gate.run_gate(db, db.get(row["uid"]), MASTER, api_key="k", jd_text="jd",
                  call=fake_call_soft)
    gap_after = db.gaps_for(row["uid"])[0]
    assert gap_after["status"] == "closed"
    assert gap_after["closed_reason"] == "reclassified"

    # A further rescan brings it back as a hard NONE.
    _gate_with_one_hard_none(db, db.get(row["uid"]), quote)

    gap_final = db.gaps_for(row["uid"])[0]
    assert gap_final["id"] == gid, "must reopen the same gap row, not add a new one"
    assert gap_final["status"] == "open"
