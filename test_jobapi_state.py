"""Advancing state is the action that made this an API instead of a spool: it
needs a synchronous yes or no. An illegal jump has to come back as an error
the UI can show, not land silently and surface later as a broken row.
"""
import pytest
from fastapi.testclient import TestClient

import jobapi
import jobdb

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    db = jobdb.JobDB(path)
    db.upsert_job({"id": "1", "ats": "greenhouse", "company": "acme",
                   "title": "Senior SRE", "location": "Remote"})
    db.close()
    monkeypatch.setenv("JOB_DB", str(path))
    monkeypatch.setenv("JOB_API_TOKEN", "test-token")
    return TestClient(jobapi.app)


@pytest.fixture
def slug():
    return jobdb.make_slug(
        "acme", "Senior SRE", jobdb.make_job_uid("greenhouse", "acme", "1"))


def test_a_legal_transition_succeeds(client, slug):
    r = client.post(f"/jobs/{slug}/state", json={"state": "queued"},
                    headers=AUTH)
    assert r.status_code == 200
    assert r.json()["state"] == "queued"


def test_advancing_state_also_marks_the_lead_read(client, slug):
    """Queueing a lead is an explicit disposition. Leaving it in the unread
    queue afterwards would be a bug."""
    r = client.post(f"/jobs/{slug}/state", json={"state": "queued"},
                    headers=AUTH)
    assert r.json()["read_at"] is not None


def test_an_illegal_transition_is_409_with_the_reason(client, slug):
    r = client.post(f"/jobs/{slug}/state", json={"state": "applied"},
                    headers=AUTH)
    assert r.status_code == 409
    assert "illegal transition" in r.json()["detail"]
    assert "discovered -> applied" in r.json()["detail"]


def test_an_illegal_transition_changes_nothing(client, slug):
    client.post(f"/jobs/{slug}/state", json={"state": "applied"}, headers=AUTH)
    r = client.get(f"/jobs/{slug}/transitions", headers=AUTH)
    assert r.json()["state"] == "discovered"
    # A rejected transition raises before set_read ever runs, so the lead
    # must still be unread. Checked through the note endpoint, not the
    # database, to stay at the API boundary like the rest of this file.
    note = client.post(f"/jobs/{slug}/note", json={}, headers=AUTH)
    assert note.json()["read_at"] is None


def test_an_unknown_state_is_400(client, slug):
    r = client.post(f"/jobs/{slug}/state", json={"state": "banana"},
                    headers=AUTH)
    assert r.status_code == 400


def test_transitions_lists_only_legal_next_states(client, slug):
    r = client.get(f"/jobs/{slug}/transitions", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"state": "discovered", "next": ["queued", "skipped"]}


def test_transitions_follows_the_lead_as_it_moves(client, slug):
    client.post(f"/jobs/{slug}/state", json={"state": "queued"}, headers=AUTH)
    r = client.get(f"/jobs/{slug}/transitions", headers=AUTH)
    assert r.json()["state"] == "queued"
    assert r.json()["next"] == ["discovered", "drafted", "skipped"]


def test_a_decided_close_offers_nothing(client, slug):
    """rejected is final, so the inbox must not draw a reopen button."""
    for s in ("queued", "drafted", "ready", "applied"):
        client.post(f"/jobs/{slug}/state", json={"state": s}, headers=AUTH)
    client.post(f"/jobs/{slug}/state",
                json={"state": "closed", "outcome": "rejected"}, headers=AUTH)
    r = client.get(f"/jobs/{slug}/transitions", headers=AUTH)
    assert r.json() == {"state": "closed", "next": []}


def test_a_ghosted_close_offers_the_reopen(client, slug):
    """Ghosted means nobody decided anything, so the lead can come back."""
    for s in ("queued", "drafted", "ready", "applied"):
        client.post(f"/jobs/{slug}/state", json={"state": s}, headers=AUTH)
    client.post(f"/jobs/{slug}/state",
                json={"state": "closed", "outcome": "ghosted"}, headers=AUTH)
    r = client.get(f"/jobs/{slug}/transitions", headers=AUTH)
    assert r.json() == {"state": "closed",
                        "next": ["applied", "interviewing"]}


def test_reposting_the_current_state_is_an_idempotent_no_op(client, slug):
    """Safe to retry. If a client queues a lead and the response is lost, the
    retry sends the same state again and must not surface an error for work
    that already succeeded. jobdb.set_state returns early when the state is
    unchanged, so this is a 200 with nothing altered.
    """
    client.post(f"/jobs/{slug}/state", json={"state": "queued"}, headers=AUTH)
    r = client.post(f"/jobs/{slug}/state", json={"state": "queued"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["state"] == "queued"


def _row(tmp_path, slug):
    """Read the row straight from the DB the client fixture is pointed at.

    tmp_path is per-test, so this opens the same file the TestClient wrote.
    The reason columns are deliberately absent from the endpoint's payload,
    so there is nothing to assert against in the response body.
    """
    db = jobdb.JobDB(tmp_path / "jobs.db")
    try:
        return db.resolve(slug)
    finally:
        db.close()


def _walk_to_applied(client, slug):
    for state in ("queued", "drafted", "ready", "applied"):
        r = client.post(f"/jobs/{slug}/state", json={"state": state},
                        headers=AUTH)
        assert r.status_code == 200, r.text


def test_a_reason_on_close_writes_close_reason(client, slug, tmp_path):
    """The API path has to write this column, or a lead closed from the
    dashboard renders a blank close reason on the detail page forever."""
    _walk_to_applied(client, slug)
    r = client.post(f"/jobs/{slug}/state",
                    json={"state": "closed", "outcome": "rejected",
                          "reason": "Rejected at decision stage."},
                    headers=AUTH)
    assert r.status_code == 200
    row = _row(tmp_path, slug)
    assert row["state"] == "closed"
    assert row["outcome"] == "rejected"
    assert row["close_reason"] == "Rejected at decision stage."
    assert row["skip_reason"] is None


def test_a_reason_on_skip_writes_skip_reason(client, slug, tmp_path):
    r = client.post(f"/jobs/{slug}/state",
                    json={"state": "skipped", "reason": "Deep Kubernetes."},
                    headers=AUTH)
    assert r.status_code == 200
    row = _row(tmp_path, slug)
    assert row["skip_reason"] == "Deep Kubernetes."
    assert row["close_reason"] is None


def test_a_reason_on_any_other_state_is_400(client, slug, tmp_path):
    """There is no structured column for it, and silently dropping a string
    the operator typed into a mandatory field is worse than refusing it. note
    is the audited free-text field and works for every state."""
    r = client.post(f"/jobs/{slug}/state",
                    json={"state": "queued", "reason": "looks good"},
                    headers=AUTH)
    assert r.status_code == 400
    assert "reason is only accepted for" in r.json()["detail"]
    assert _row(tmp_path, slug)["state"] == "discovered"


def test_a_refused_transition_writes_no_reason(client, slug, tmp_path):
    """set_state raises before it writes anything, so a 409 leaves the reason
    columns untouched instead of stamping a reason onto a state that never
    happened."""
    r = client.post(f"/jobs/{slug}/state",
                    json={"state": "closed", "outcome": "rejected",
                          "reason": "should not land"},
                    headers=AUTH)
    assert r.status_code == 409
    row = _row(tmp_path, slug)
    assert row["state"] == "discovered"
    assert row["close_reason"] is None


def test_note_and_reason_are_independent(client, slug, tmp_path):
    _walk_to_applied(client, slug)
    client.post(f"/jobs/{slug}/state",
                json={"state": "closed", "outcome": "ghosted",
                      "note": "audit line", "reason": "structured line"},
                headers=AUTH)
    row = _row(tmp_path, slug)
    assert row["close_reason"] == "structured line"
    db = jobdb.JobDB(tmp_path / "jobs.db")
    try:
        notes = [e["note"] for e in db.history(row["uid"])]
    finally:
        db.close()
    assert "audit line" in notes


def test_a_state_write_without_a_reason_still_works(client, slug, tmp_path):
    """The field is optional. Every existing caller omits it."""
    r = client.post(f"/jobs/{slug}/state", json={"state": "queued"},
                    headers=AUTH)
    assert r.status_code == 200
    assert _row(tmp_path, slug)["skip_reason"] is None


def test_the_wire_format_mission_control_actually_sends(client, slug, tmp_path):
    """lib/job-api.ts postState always sends all four keys, using null for the
    ones it has no value for. An explicit null must behave exactly like an
    omitted field, or every forward move from the dashboard would 400."""
    r = client.post(f"/jobs/{slug}/state",
                    json={"state": "queued", "note": None, "outcome": None,
                          "reason": None},
                    headers=AUTH)
    assert r.status_code == 200
    assert _row(tmp_path, slug)["state"] == "queued"


def test_the_wire_format_for_a_close(client, slug, tmp_path):
    _walk_to_applied(client, slug)
    r = client.post(f"/jobs/{slug}/state",
                    json={"state": "closed", "note": "Rejected at decision stage.",
                          "outcome": "rejected",
                          "reason": "Rejected at decision stage."},
                    headers=AUTH)
    assert r.status_code == 200
    row = _row(tmp_path, slug)
    assert (row["state"], row["outcome"]) == ("closed", "rejected")
    assert row["close_reason"] == "Rejected at decision stage."


def test_a_ghosted_lead_actually_reopens_through_the_api(client, slug):
    """End to end, not just the button list: the inbox can reopen a lead that
    came back, and the response reports the live state."""
    for s in ("queued", "drafted", "ready", "applied"):
        client.post(f"/jobs/{slug}/state", json={"state": s}, headers=AUTH)
    client.post(f"/jobs/{slug}/state",
                json={"state": "closed", "outcome": "ghosted"}, headers=AUTH)
    r = client.post(f"/jobs/{slug}/state",
                    json={"state": "interviewing",
                          "note": "recruiter came back"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["state"] == "interviewing"


def test_a_rejected_lead_cannot_reopen_through_the_api(client, slug):
    for s in ("queued", "drafted", "ready", "applied"):
        client.post(f"/jobs/{slug}/state", json={"state": s}, headers=AUTH)
    client.post(f"/jobs/{slug}/state",
                json={"state": "closed", "outcome": "rejected"}, headers=AUTH)
    r = client.post(f"/jobs/{slug}/state",
                    json={"state": "interviewing"}, headers=AUTH)
    assert r.status_code == 409


def test_an_outcome_outside_a_close_is_a_bad_request(client, slug):
    """Mirrors the `reason` handling directly above it in post_state. A live
    row carrying an outcome would stay eligible to reopen forever."""
    for s in ("queued", "drafted", "ready", "applied"):
        client.post(f"/jobs/{slug}/state", json={"state": s}, headers=AUTH)
    client.post(f"/jobs/{slug}/state",
                json={"state": "closed", "outcome": "ghosted"}, headers=AUTH)
    r = client.post(f"/jobs/{slug}/state",
                    json={"state": "interviewing", "outcome": "ghosted"},
                    headers=AUTH)
    assert r.status_code == 400
    assert "outcome" in r.json()["detail"]


def test_a_reopen_through_the_api_leaves_no_outcome_behind(client, slug):
    for s in ("queued", "drafted", "ready", "applied"):
        client.post(f"/jobs/{slug}/state", json={"state": s}, headers=AUTH)
    client.post(f"/jobs/{slug}/state",
                json={"state": "closed", "outcome": "ghosted"}, headers=AUTH)
    client.post(f"/jobs/{slug}/state", json={"state": "interviewing"},
                headers=AUTH)
    r = client.get(f"/jobs/{slug}/transitions", headers=AUTH)
    assert r.json()["state"] == "interviewing"
