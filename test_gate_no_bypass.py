"""Regression tests for the two CRITICAL bypasses found in the final pre-merge
review of the Fit Gate (feature/fit-gate):

1. gate-rule let a human reclassify ANY requirement, including confident hard
   NONEs and ledger-forced ones, with no mandatory reason. That defeats
   do_not_claim, the one thing the design calls "absolute".
2. An override survived a later, worse gate re-run, so overriding a transient
   ERROR (no API key, unfetchable JD) could leave drafting open forever even
   after a fresh gate run came back DO_NOT_APPLY.

Both are reproduced here against the current code first (this file is written
before the fix, per the TDD workflow) and must fail before the fix lands.
"""
import json

import pytest

import gate
import jobdb
import job_cli


def _db(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"ats": "greenhouse", "company": "globex", "id": "1",
                   "title": "Senior Staff Operations Engineer", "location": "Remote"})
    return db, db.get(jobdb.make_job_uid("greenhouse", "globex", "1"))


def test_a_confident_hard_none_cannot_be_ruled_soft(tmp_path):
    """The reclassification bypass. Three confident hard NONEs must not be
    into a PROCEED. Only an UNSURE classification is adjudicable."""
    db, row = _db(tmp_path)
    reqs = [
        {"quote": "Deep expertise in data catalog architecture",
         "topic": "data catalog architecture", "hard": True, "confidence": "high",
         "verdict": "NONE", "evidence": "", "bridge": "", "forced": "",
         "ruled_by_human": False},
        {"quote": "Deep expertise in ... correlation", "topic": "correlation",
         "hard": True, "confidence": "high", "verdict": "NONE", "evidence": "",
         "bridge": "", "forced": "", "ruled_by_human": False},
        {"quote": "Proficient in Python or Go", "topic": "python or go",
         "hard": True, "confidence": "high", "verdict": "NONE", "evidence": "",
         "bridge": "", "forced": "", "ruled_by_human": False},
    ]
    db.set_gate(row["uid"], gate.DO_NOT_APPLY,
                json.dumps({"requirements": reqs, "title": {}}), str(tmp_path / "r.md"))
    baseline = gate.counts(reqs)
    assert baseline["known_hard_none"] == 3
    assert gate.decide(reqs) == gate.DO_NOT_APPLY

    for n in (1, 2, 3):
        args = type("A", (), {"ident": row["slug"], "n": n, "hard": False,
                              "soft": True, "note": "reads as soft to me"})()
        with pytest.raises(SystemExit):
            job_cli.cmd_gate_rule(db, args)

    after = db.get(row["uid"])
    assert after["gate_decision"] == gate.DO_NOT_APPLY, (
        "a confident hard NONE was reclassified soft; the bypass is still open")
    assert (after["gate_override_reason"] or "") == "", (
        "gate-rule must never itself write an override reason")
    saved = json.loads(after["gate_json"])["requirements"]
    assert all(r["hard"] for r in saved), "no requirement should have flipped to soft"


def test_a_ledger_forced_requirement_cannot_be_ruled_at_all(tmp_path):
    """do_not_claim is absolute. Not the model, not the human classification."""
    db, row = _db(tmp_path)
    reqs = [
        {"quote": "Deep expertise in data catalog architecture",
         "topic": "data catalog architecture", "hard": True, "confidence": "low",
         "verdict": "NONE", "evidence": "", "bridge": "",
         "forced": "do-not-claim: data catalog architecture",
         "ruled_by_human": False},
    ]
    db.set_gate(row["uid"], gate.CONDITIONAL,
                json.dumps({"requirements": reqs, "title": {}}), str(tmp_path / "r.md"))

    args = type("A", (), {"ident": row["slug"], "n": 1, "hard": False,
                          "soft": True, "note": "I think this one is actually fine"})()
    with pytest.raises(SystemExit):
        job_cli.cmd_gate_rule(db, args)

    after = json.loads(db.get(row["uid"])["gate_json"])["requirements"]
    assert after[0]["verdict"] == "NONE"
    assert after[0]["hard"] is True
    assert after[0]["ruled_by_human"] is False, (
        "a ledger-forced requirement must never be marked ruled_by_human")


def test_an_override_does_not_survive_a_later_gate_run(tmp_path, monkeypatch):
    """Override an ERROR, then let the gate run properly and return DO_NOT_APPLY.
    The stale override must NOT keep the job draftable."""
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 7, 14, 10, 0, 0, tzinfo=timezone.utc)
    counter = {"n": 0}

    def fake_now():
        counter["n"] += 1
        return (base + timedelta(seconds=counter["n"])).isoformat(timespec="seconds")

    monkeypatch.setattr(jobdb, "now_iso", fake_now)

    db, row = _db(tmp_path)
    # Simulate an ERROR gate (e.g. no API key), then an override to get one
    # package out.
    db.set_gate(row["uid"], gate.ERROR,
                json.dumps({"requirements": [], "title": {}}), str(tmp_path / "r.md"))
    db.set_override(row["uid"], "recruiter said the coding bar is waived")
    gate.require_pass(db, db.get(row["uid"]))  # the fresh override holds; must not raise

    # The key gets fixed and the gate later runs properly against the (now
    # worse-looking, or just re-read) JD.
    def fake_call(system, user, api_key):
        return json.dumps({"requirements": [
            {"quote": "q1", "topic": "t1", "hard": True, "confidence": "high",
             "verdict": "NONE", "evidence": "", "bridge": ""},
            {"quote": "q2", "topic": "t2", "hard": True, "confidence": "high",
             "verdict": "NONE", "evidence": "", "bridge": ""},
        ]})

    master = {"works_as": [], "capabilities": [], "do_not_claim": []}
    out = gate.run_gate(db, db.get(row["uid"]), master, api_key="k", jd_text="jd",
                        call=fake_call)
    assert out["decision"] == gate.DO_NOT_APPLY

    with pytest.raises(gate.GateBlocked):
        gate.require_pass(db, db.get(row["uid"]))
