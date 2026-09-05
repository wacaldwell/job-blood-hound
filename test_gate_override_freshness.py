"""Regression tests for two override holes found in review of the Fit Gate's
override-freshness check in gate.require_pass.

1. THE TIE. jobdb.now_iso() is second-granular. Override an ERROR gate, then
   let a fresh gate run land on the IDENTICAL wall-clock second (both are
   fast, no-network paths: an ERROR gate is a fast fail, and a DO_NOT_APPLY
   is reachable via a fast fail path too). gate_overridden_at ==
   gate_at as strings, and `>=` read the tie as "override still valid".
   require_pass did not raise despite a fresh DO_NOT_APPLY.

2. THE NEVER-GATED HOLE. cmd_gate_override never checked that the job was
   ever gated, and set_override was an unconditional UPDATE. When gate_at is
   NULL, `(job_row["gate_at"] or "")` is "", and any non-empty
   gate_overridden_at is lexicographically >= "", so the freshness check was
   vacuously satisfied. A job the gate never evaluated became draftable via
   an override alone.

The fix removes the timestamp comparison entirely: jobdb.set_gate() clears
any prior override on every fresh decision (an override waives a SPECIFIC
decision; a new decision is a new fact), and require_pass requires job_row
["gate_at"] to be truthy before honoring a surviving override.
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


def test_a_same_second_override_does_not_survive_a_later_worse_gate(tmp_path, monkeypatch):
    """The tie. now_iso is second-granular, so an override and a re-gate can land
    on the IDENTICAL timestamp. Pin now_iso to a FIXED value (not an incrementing
    fake clock, which is exactly the blind spot that let this through in the
    existing regression test)."""
    monkeypatch.setattr(jobdb, "now_iso", lambda: "2026-07-14T10:00:00+00:00")

    db, row = _db(tmp_path)
    # Fast, no-network path: an ERROR gate (e.g. no API key).
    db.set_gate(row["uid"], gate.ERROR, "{}", "/tmp/r.md")
    db.set_override(row["uid"], "recruiter said the coding bar is waived")
    gate.require_pass(db, db.get(row["uid"]))  # the fresh override holds; must not raise

    # A fresh gate run, in the SAME (frozen) second, comes back worse.
    db.set_gate(row["uid"], gate.DO_NOT_APPLY, "{}", "/tmp/r2.md")

    with pytest.raises(gate.GateBlocked):
        gate.require_pass(db, db.get(row["uid"]))


def test_an_override_cannot_stand_in_for_a_gate_that_never_ran(tmp_path):
    """gate_at NULL. An override must not make a never-evaluated job draftable."""
    db, row = _db(tmp_path)

    # Write an override directly (bypassing set_override's own never-gated
    # guard) to prove require_pass itself refuses a never-gated override,
    # not just the CLI/db layer above it.
    db.conn.execute(
        "UPDATE jobs SET gate_override_reason = ?, gate_overridden_at = ? "
        "WHERE uid = ?",
        ("recruiter said so", jobdb.now_iso(), row["uid"]))
    db.conn.commit()

    with pytest.raises(gate.GateBlocked):
        gate.require_pass(db, db.get(row["uid"]))

    # jobdb.set_override refuses to write an override for a job with no
    # gate_decision at all (defense in depth).
    with pytest.raises(ValueError):
        db.set_override(row["uid"], "recruiter said so")

    # And the CLI refuses too.
    args = type("A", (), {"ident": row["slug"], "reason": "recruiter said so"})()
    with pytest.raises(SystemExit):
        job_cli.cmd_gate_override(db, args)


def test_a_fresh_gate_decision_clears_a_prior_override(tmp_path):
    """The mechanism. A new decision is a new fact; the old waiver is void."""
    db, row = _db(tmp_path)
    db.set_gate(row["uid"], gate.DO_NOT_APPLY, "{}", "/tmp/r.md")
    db.set_override(row["uid"], "Recruiter reached out directly.")
    gate.require_pass(db, db.get(row["uid"]))  # draftable, no re-gate yet

    db.set_gate(row["uid"], gate.DO_NOT_APPLY, "{}", "/tmp/r2.md")  # re-gate

    after = db.get(row["uid"])
    assert after["gate_override_reason"] is None
    with pytest.raises(gate.GateBlocked):
        gate.require_pass(db, after)


def test_a_recompute_after_a_ruling_also_clears_the_override(tmp_path):
    """Conservative and deliberate: a recompute is a new decision too."""
    db, row = _db(tmp_path)
    reqs = [{"quote": "q", "topic": "t", "hard": True, "confidence": "high",
             "verdict": "NONE", "evidence": "", "bridge": "", "forced": "",
             "ruled_by_human": False}]
    db.set_gate(row["uid"], gate.DO_NOT_APPLY,
                json.dumps({"requirements": reqs, "title": {}}), "/tmp/r.md")
    db.set_override(row["uid"], "Recruiter reached out directly.")
    gate.require_pass(db, db.get(row["uid"]))  # draftable before recompute

    gate.recompute(db, db.get(row["uid"]))

    after = db.get(row["uid"])
    assert after["gate_override_reason"] is None
    with pytest.raises(gate.GateBlocked):
        gate.require_pass(db, after)


def test_an_override_still_unblocks_when_no_regate_happened(tmp_path):
    """The legitimate path must keep working: gate -> DO_NOT_APPLY, override,
    then draft with no re-gate in between."""
    db, row = _db(tmp_path)
    db.set_gate(row["uid"], gate.DO_NOT_APPLY, "{}", "/tmp/r.md")
    db.set_override(row["uid"], "Recruiter reached out directly.")
    gate.require_pass(db, db.get(row["uid"]))  # does not raise
