"""Closing a gap unblocks drafting, so it is a decision, and a decision goes
on the record, the same rule gate-override already follows.

Reproduces the hole a reviewer found live: `jh gap-close` took no plan, no
reason, and wrote no audit row, and require_pass only inspects OPEN gaps. So a
CONDITIONAL job with an OPEN, unplanned gap (no plan, no hours, no deadline)
could be made draftable by closing the gap with nothing on the record about
why.
"""
import pytest
import gate
import jobdb
import job_cli


def _db(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"ats": "greenhouse", "company": "acme", "id": "1",
                   "title": "Engineer", "location": "Remote"})
    return db, db.get(jobdb.make_job_uid("greenhouse", "acme", "1"))


def test_closing_a_gap_with_no_reason_is_refused(tmp_path):
    """Closing a gap unblocks drafting, so it is a decision, and a decision goes
    on the record. Same rule as gate-override."""
    db, row = _db(tmp_path)
    gid = db.add_gap(row["uid"], "Kubernetes at scale")

    # jobdb level: empty, whitespace, and None must all raise ValueError
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            db.close_gap(gid, bad)
    assert db.gaps_for(row["uid"])[0]["status"] == "open"

    # CLI level: a whitespace-only --reason must exit nonzero
    args = type("A", (), {"gap_id": gid, "reason": "   "})()
    with pytest.raises(SystemExit):
        job_cli.cmd_gap_close(db, args)
    assert db.gaps_for(row["uid"])[0]["status"] == "open"


def test_closing_a_gap_is_audited_in_state_log(tmp_path):
    """A closed gap unblocked a job. `jh show` history must say so."""
    db, row = _db(tmp_path)
    gid = db.add_gap(row["uid"], "Kubernetes at scale")
    db.close_gap(gid, "Studied enough to speak to it in interviews.")
    notes = [h["note"] or "" for h in db.history(row["uid"])]
    assert any("gap" in n.lower() and "closed" in n.lower() for n in notes)
    assert any("Studied enough" in n for n in notes)


def test_the_system_auto_close_needs_no_reason_and_writes_no_audit_row(tmp_path):
    """close_gaps_not_in is the reconciler, not a human decision. It must stay
    reasonless and silent, or every gate re-run would spam the audit trail."""
    db, row = _db(tmp_path)
    db.add_gap(row["uid"], "reclassified to soft")
    before = len(db.history(row["uid"]))
    n = db.close_gaps_not_in(row["uid"], set())
    assert n == 1
    assert len(db.history(row["uid"])) == before


def test_gap_close_still_records_why_the_job_became_draftable(tmp_path):
    """The reproduction. Close a gap with a reason, and the reason is retrievable
    from the gap row and from state_log. Before this fix, a gap could be closed
    with no plan, no hours, no deadline and no record at all, and the job drafted."""
    db, row = _db(tmp_path)
    db.set_gate(row["uid"], gate.CONDITIONAL, "{}", "/tmp/r.md")
    gid = db.add_gap(row["uid"], "Proficient in Kubernetes at scale")

    with pytest.raises(gate.GateBlocked, match="gap"):
        gate.require_pass(db, db.get(row["uid"]))

    reason = "Recruiter confirmed this is nice to have, not required for round 1."
    db.close_gap(gid, reason)

    gate.require_pass(db, db.get(row["uid"]))  # now allowed

    gap = db.gaps_for(row["uid"])[0]
    assert gap["close_note"] == reason
    notes = [h["note"] or "" for h in db.history(row["uid"])]
    assert any(reason in n for n in notes)
