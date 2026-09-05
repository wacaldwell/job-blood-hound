import json
import pytest
import gate
import jobdb
import job_generate


def _db(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"ats": "greenhouse", "company": "globex", "id": "1",
                   "title": "Senior Staff Operations Engineer", "location": "Remote"})
    return db, db.get(jobdb.make_job_uid("greenhouse", "globex", "1"))


def test_an_ungated_job_cannot_draft(tmp_path):
    """The 363 jobs already in the live DB have never been gated. Fail closed."""
    db, row = _db(tmp_path)
    with pytest.raises(gate.GateBlocked, match="has not been gated"):
        gate.require_pass(db, row)


def test_do_not_apply_blocks(tmp_path):
    db, row = _db(tmp_path)
    db.set_gate(row["uid"], gate.DO_NOT_APPLY, "{}", "/tmp/r.md")
    with pytest.raises(gate.GateBlocked, match="DO NOT APPLY"):
        gate.require_pass(db, db.get(row["uid"]))


def test_error_blocks_too(tmp_path):
    db, row = _db(tmp_path)
    db.set_gate(row["uid"], gate.ERROR, "{}", "/tmp/r.md")
    with pytest.raises(gate.GateBlocked):
        gate.require_pass(db, db.get(row["uid"]))


def test_proceed_allows(tmp_path):
    db, row = _db(tmp_path)
    db.set_gate(row["uid"], gate.PROCEED, "{}", "/tmp/r.md")
    gate.require_pass(db, db.get(row["uid"]))  # does not raise


def test_conditional_blocks_until_every_gap_is_planned(tmp_path):
    db, row = _db(tmp_path)
    db.set_gate(row["uid"], gate.CONDITIONAL, "{}", "/tmp/r.md")
    gid = db.add_gap(row["uid"], "Proficient in Python or Go")

    with pytest.raises(gate.GateBlocked, match="gap"):
        gate.require_pass(db, db.get(row["uid"]))

    db.plan_gap(gid, "Go tour, build one CLI.", 20, "2026-08-01")
    gate.require_pass(db, db.get(row["uid"]))  # now allowed


def test_an_override_unblocks_anything(tmp_path):
    db, row = _db(tmp_path)
    db.set_gate(row["uid"], gate.DO_NOT_APPLY, "{}", "/tmp/r.md")
    db.set_override(row["uid"], "Recruiter reached out directly.")
    gate.require_pass(db, db.get(row["uid"]))  # does not raise


def test_unknown_gate_decision_blocks(tmp_path):
    """An unrecognized decision string must BLOCK, not fall through to allow.
    An if/elif chain with no final raise would fail OPEN here."""
    db, row = _db(tmp_path)
    db.set_gate(row["uid"], "SOMETHING_NEW", "{}", "/tmp/r.md")
    with pytest.raises(gate.GateBlocked):
        gate.require_pass(db, db.get(row["uid"]))


def test_whitespace_only_override_does_not_unblock(tmp_path):
    """A blank reason is not a reason. If you cannot write one, that is the answer."""
    db, row = _db(tmp_path)
    db.set_gate(row["uid"], gate.DO_NOT_APPLY, "{}", "/tmp/r.md")
    db.conn.execute("UPDATE jobs SET gate_override_reason = '   ' WHERE uid = ?",
                    (row["uid"],))
    db.conn.commit()
    with pytest.raises(gate.GateBlocked):
        gate.require_pass(db, db.get(row["uid"]))


def test_needs_review_blocks(tmp_path):
    db, row = _db(tmp_path)
    db.set_gate(row["uid"], gate.NEEDS_REVIEW, "{}", "/tmp/r.md")
    with pytest.raises(gate.GateBlocked):
        gate.require_pass(db, db.get(row["uid"]))


def test_generate_refuses_to_produce_artifacts_for_a_blocked_job(tmp_path, monkeypatch):
    """The guard must live in generate(), so the MCP/Discord path inherits it.
    If this test passes only because the CLI checks, the Discord path is open."""
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    db, row = _db(tmp_path)
    db.set_gate(row["uid"], gate.DO_NOT_APPLY, "{}", "/tmp/r.md")

    with pytest.raises(gate.GateBlocked):
        job_generate.generate(db, db.get(row["uid"]))
