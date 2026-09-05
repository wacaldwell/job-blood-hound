"""A ghosted job that comes back to life must be recordable, and only that one.

`closed` was flatly terminal, which is right for a decision that was actually
communicated (rejected, offer, accepted, withdrawn) and wrong for `ghosted`.
Ghosted does not mean "they said no", it means "they stopped replying", and a
recruiter who goes quiet for two weeks and then books a round has not made any
decision to be terminal about.

Live case (2026-08-31): one recruiter screen was held 2026-08-07, the loop
went silent, and the job was closed `ghosted` on 2026-08-20. Eleven days later
the recruiter came back to schedule round 1. There was no audited way to say
so; the only options were a raw UPDATE on the host (which bypasses the single
writer and leaves no state_log row) or losing the loop's whole history to a new
row.

The guard is deliberately narrow. Reopening a job closed as `rejected` would
silently corrupt the Reply Window numbers, which measure how long employers
take to respond, so only an explicit `ghosted` outcome reopens; anything else,
including a close with no recorded outcome, stays terminal.
"""
import pytest

import jobdb
from jobdb import JobDB, TransitionError


def _closed_job(tmp_path, outcome="ghosted", ident="acme", reason=None):
    """A job walked all the way to closed with the given outcome."""
    db = JobDB(tmp_path / "t.db")
    db.upsert_job({
        "id": ident, "ats": "greenhouse", "company": ident,
        "title": "Manager, Cloud Engineering", "location": "Remote",
        "url": f"https://example.com/{ident}",
    })
    uid = jobdb.make_job_uid("greenhouse", ident, ident)
    for s in ("queued", "drafted", "ready", "applied", "interviewing"):
        db.set_state(uid, s)
    db.set_state(uid, "closed", outcome=outcome, reason=reason)
    return db, uid


# -- the reopen that is allowed -----------------------------------------

def test_ghosted_job_reopens_to_interviewing(tmp_path):
    db, uid = _closed_job(tmp_path)
    row = db.set_state(uid, "interviewing", note="recruiter came back")
    assert row["state"] == "interviewing"


def test_ghosted_job_reopens_to_applied(tmp_path):
    """A lead that never reached a round reopens to where it actually was."""
    db, uid = _closed_job(tmp_path)
    row = db.set_state(uid, "applied", note="recruiter came back")
    assert row["state"] == "applied"


# -- what the reopen has to clean up ------------------------------------

def test_reopening_clears_the_close_columns(tmp_path):
    """A live row must not still claim it is closed and ghosted.

    closed_at feeds the Reply Window stats and `outcome` is what the guard
    below reads, so leaving either behind would let a reopened job be counted
    as closed and then reopened a second time from a state it is no longer in.
    """
    db, uid = _closed_job(tmp_path, reason="no reply for 13 days")
    db.set_state(uid, "interviewing")
    row = db.get(uid)
    assert row["outcome"] is None
    assert row["closed_at"] is None
    assert row["close_reason"] is None


def test_reopening_keeps_the_close_in_the_audit_trail(tmp_path):
    """The silence really happened; the history is what remembers it."""
    db, uid = _closed_job(tmp_path)
    db.set_state(uid, "interviewing", note="recruiter came back")
    log = db.history(uid)
    pairs = [(r["from_state"], r["to_state"]) for r in log]
    assert ("interviewing", "closed") in pairs
    assert ("closed", "interviewing") in pairs
    assert [r["note"] for r in log if r["to_state"] == "interviewing"
            and r["from_state"] == "closed"] == ["recruiter came back"]


# -- what stays terminal -------------------------------------------------

@pytest.mark.parametrize("outcome", ["rejected", "withdrawn", "offer",
                                     "accepted", "other"])
def test_a_decided_close_is_still_terminal(tmp_path, outcome):
    db, uid = _closed_job(tmp_path, outcome=outcome)
    with pytest.raises(TransitionError) as e:
        db.set_state(uid, "interviewing")
    assert outcome in str(e.value)


def test_a_close_with_no_outcome_is_still_terminal(tmp_path):
    """Fail closed: only an explicit `ghosted` reopens, never an unknown."""
    db, uid = _closed_job(tmp_path, outcome=None)
    with pytest.raises(TransitionError):
        db.set_state(uid, "interviewing")


@pytest.mark.parametrize("to_state", ["discovered", "queued", "drafted",
                                      "ready", "skipped"])
def test_reopen_cannot_skip_back_up_the_pipeline(tmp_path, to_state):
    """Reopening restores a live conversation, it does not re-run the funnel."""
    db, uid = _closed_job(tmp_path)
    with pytest.raises(TransitionError):
        db.set_state(uid, to_state)


def test_reopening_does_not_restamp_the_original_application_date(tmp_path):
    """applied_at is when the human actually submitted, and the Reply Window
    measures employer response time from it. Restamping it on reopen would
    make a 13-day silence read as an instant reply."""
    db, uid = _closed_job(tmp_path)
    before = db.get(uid)["applied_at"]
    db.set_state(uid, "applied", note="recruiter came back")
    assert db.get(uid)["applied_at"] == before


# -- the guard cannot be walked around ------------------------------------

@pytest.mark.parametrize("column", ["state", "outcome", "closed_at"])
def test_set_fields_refuses_the_lifecycle_columns(tmp_path, column):
    """set_fields is an unaudited UPDATE, so it must not be able to stage a
    reopen. Flipping a rejected row's outcome to 'ghosted' there would satisfy
    the guard on the next set_state call and leave nothing in state_log saying
    the outcome was ever changed."""
    db, uid = _closed_job(tmp_path, outcome="rejected")
    with pytest.raises(ValueError) as e:
        db.set_fields(uid, **{column: "ghosted"})
    assert "set_state" in str(e.value)
    assert db.get(uid)["outcome"] == "rejected"


def test_set_fields_still_writes_ordinary_columns(tmp_path):
    """The blocklist must not have caught the fields that legitimately use it,
    close_reason among them (job_hound_mcp writes it beside a close)."""
    db, uid = _closed_job(tmp_path)
    db.set_fields(uid, notes="called back", close_reason="no reply")
    row = db.get(uid)
    assert row["notes"] == "called back"
    assert row["close_reason"] == "no reply"


# -- outcome belongs to a close, and only to a close ---------------------

def test_reopening_with_an_outcome_is_refused(tmp_path):
    """Found in review of this change. `outcome` was appended to the same
    UPDATE as the reopen's `outcome = NULL`, and SQLite took the later
    assignment, so the row came back live still saying 'ghosted'. Closing it
    again later without an explicit outcome would leave that stale 'ghosted'
    in place and make it reopenable forever, with no ghosted disposition ever
    actually recorded."""
    db, uid = _closed_job(tmp_path)
    with pytest.raises(TransitionError) as e:
        db.set_state(uid, "interviewing", outcome="ghosted")
    assert "outcome" in str(e.value)
    assert db.get(uid)["state"] == "closed"


# Each destination paired with the path that legally reaches it, so the guard
# is what fires rather than the transition check sitting above it.
_PATHS = [
    ((), "queued"),
    ((), "skipped"),
    (("queued",), "drafted"),
    (("queued", "drafted"), "ready"),
    (("queued", "drafted", "ready"), "applied"),
    (("queued", "drafted", "ready", "applied"), "interviewing"),
]


@pytest.mark.parametrize("walk,to_state", _PATHS)
def test_an_outcome_is_refused_for_every_non_closed_state(tmp_path, walk,
                                                          to_state):
    """An outcome is what a close records. Anywhere else it is a caller bug,
    and answering it loudly beats writing a live row that claims a verdict."""
    db = JobDB(tmp_path / "t.db")
    db.upsert_job({"id": "b", "ats": "greenhouse", "company": "b",
                   "title": "T", "location": "R", "url": "u"})
    uid = jobdb.make_job_uid("greenhouse", "b", "b")
    for s in walk:
        db.set_state(uid, s)
    with pytest.raises(TransitionError) as e:
        db.set_state(uid, to_state, outcome="rejected")
    assert "outcome" in str(e.value)


def test_a_close_still_takes_its_outcome(tmp_path):
    """The guard must not break the one case that is legitimate."""
    db = JobDB(tmp_path / "t.db")
    db.upsert_job({"id": "c", "ats": "greenhouse", "company": "c",
                   "title": "T", "location": "R", "url": "u"})
    uid = jobdb.make_job_uid("greenhouse", "c", "c")
    for s in ("queued", "drafted", "ready", "applied"):
        db.set_state(uid, s)
    row = db.set_state(uid, "closed", outcome="rejected")
    assert row["outcome"] == "rejected"


def test_a_falsy_outcome_is_not_treated_as_a_caller_error(tmp_path):
    """The API forwards `outcome: null` on every state post, so None must
    stay a silent no-op rather than a 409 on ordinary transitions."""
    db = JobDB(tmp_path / "t.db")
    db.upsert_job({"id": "d", "ats": "greenhouse", "company": "d",
                   "title": "T", "location": "R", "url": "u"})
    uid = jobdb.make_job_uid("greenhouse", "d", "d")
    assert db.set_state(uid, "queued", outcome=None)["state"] == "queued"
