import argparse
import jobdb
import job_cli


def _db_with_job(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"id": "1", "ats": "greenhouse", "company": "acme",
                   "title": "Senior SRE", "location": "Remote", "url": "http://x"})
    return db, jobdb.make_job_uid("greenhouse", "acme", "1")


def test_skip_stores_reason(tmp_path):
    db, uid = _db_with_job(tmp_path)
    args = argparse.Namespace(ident="acme", note=None, reason="too code-heavy")
    job_cli.cmd_skip(db, args)
    assert db.get(uid)["skip_reason"] == "too code-heavy"
    db.close()


def test_close_stores_reason(tmp_path):
    db, uid = _db_with_job(tmp_path)
    db.set_state(uid, "queued"); db.set_state(uid, "drafted")
    db.set_state(uid, "ready"); db.set_state(uid, "applied")
    args = argparse.Namespace(ident="acme", note=None,
                              outcome="rejected", reason="no response after screen")
    job_cli.cmd_close(db, args)
    row = db.get(uid)
    assert row["state"] == "closed"
    assert row["close_reason"] == "no response after screen"
    db.close()


# -- set_state writes the reason in the same transaction ---------------------
#
# The reason used to be a second, separately committed set_fields call after
# the transition (both here and in jobapi). A crash between the two commits
# left a terminal row with no reason, and `closed` has no outgoing action to
# repair it with. Now the state, the audit row, and the reason land together.


def test_set_state_writes_close_reason_in_one_call(tmp_path):
    db, uid = _db_with_job(tmp_path)
    for s in ("queued", "drafted", "ready", "applied"):
        db.set_state(uid, s)
    db.set_state(uid, "closed", outcome="rejected", reason="decision stage")
    row = db.get(uid)
    assert row["state"] == "closed"
    assert row["outcome"] == "rejected"
    assert row["close_reason"] == "decision stage"
    db.close()


def test_set_state_writes_skip_reason_in_one_call(tmp_path):
    db, uid = _db_with_job(tmp_path)
    db.set_state(uid, "skipped", reason="deep kubernetes")
    assert db.get(uid)["skip_reason"] == "deep kubernetes"
    db.close()


def test_set_state_refuses_a_reason_for_a_state_with_no_column(tmp_path):
    db, uid = _db_with_job(tmp_path)
    try:
        db.set_state(uid, "queued", reason="nowhere to put this")
    except jobdb.TransitionError as e:
        assert "reason" in str(e)
    else:
        raise AssertionError("expected a TransitionError")
    assert db.get(uid)["state"] == "discovered"
    db.close()


def test_an_illegal_transition_writes_no_reason(tmp_path):
    db, uid = _db_with_job(tmp_path)
    try:
        db.set_state(uid, "closed", outcome="rejected", reason="should not land")
    except jobdb.TransitionError:
        pass
    row = db.get(uid)
    assert row["state"] == "discovered"
    assert row["close_reason"] is None
    db.close()


def test_a_no_op_repost_does_not_rewrite_the_reason(tmp_path):
    """set_state returns early when the state is unchanged, so a retry after a
    lost response does not touch the reason already stored. Amending a reason
    is not an operation this path supports, and no UI offers one: a closed
    lead has no outgoing transition at all."""
    db, uid = _db_with_job(tmp_path)
    db.set_state(uid, "skipped", reason="first")
    db.set_state(uid, "skipped", reason="second")
    assert db.get(uid)["skip_reason"] == "first"
    db.close()
