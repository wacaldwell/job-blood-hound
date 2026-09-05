"""The write API is the only way the lead inbox reaches the database. Every
endpoint is a thin wrapper over an audited jobdb setter, so these tests cover
the wrapper: auth, identifier resolution, and status codes.
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


def test_a_request_without_a_token_is_rejected(client, slug):
    r = client.post(f"/jobs/{slug}/read", json={"read": True})
    assert r.status_code == 401


def test_a_request_with_the_wrong_token_is_rejected(client, slug):
    r = client.post(f"/jobs/{slug}/read", json={"read": True},
                    headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_an_unknown_identifier_is_404(client):
    r = client.post("/jobs/nosuchjob/read", json={"read": True}, headers=AUTH)
    assert r.status_code == 404


def test_read_marks_the_lead_processed(client, slug):
    r = client.post(f"/jobs/{slug}/read", json={"read": True}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["read_at"] is not None


def test_read_false_returns_it_to_the_queue(client, slug):
    client.post(f"/jobs/{slug}/read", json={"read": True}, headers=AUTH)
    r = client.post(f"/jobs/{slug}/read", json={"read": False}, headers=AUTH)
    assert r.json()["read_at"] is None


def test_vote_records_a_vote_and_its_note(client, slug):
    r = client.post(f"/jobs/{slug}/vote",
                    json={"vote": "up", "note": "great scope"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["vote"] == "up"
    assert r.json()["vote_note"] == "great scope"


def test_clearing_a_vote_is_allowed(client, slug):
    client.post(f"/jobs/{slug}/vote", json={"vote": "up"}, headers=AUTH)
    r = client.post(f"/jobs/{slug}/vote", json={"vote": None}, headers=AUTH)
    assert r.json()["vote"] is None


def test_an_invalid_vote_is_400(client, slug):
    r = client.post(f"/jobs/{slug}/vote", json={"vote": "sideways"},
                    headers=AUTH)
    assert r.status_code == 400


def test_an_over_long_vote_note_is_truncated_not_rejected(client, slug):
    """vote_note is a one-liner and this API is where that is enforced. A long
    paste loses its tail rather than losing the vote."""
    r = client.post(f"/jobs/{slug}/vote",
                    json={"vote": "up", "note": "z" * 5000}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["vote_note"] == "z" * jobapi.VOTE_NOTE_MAX


def test_note_writes_the_notes_column(client, slug):
    r = client.post(f"/jobs/{slug}/note",
                    json={"text": "Recruiter emailed directly."}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["notes"] == "Recruiter emailed directly."


def test_a_unique_slug_prefix_resolves(client, slug):
    r = client.post(f"/jobs/{slug[:12]}/read", json={"read": True},
                    headers=AUTH)
    assert r.status_code == 200


def test_a_missing_job_db_is_503_not_a_new_database(monkeypatch, slug):
    """Misconfiguration fails closed. Creating a database at some default path
    is the failure mode docs/single-source-of-truth.md exists to prevent."""
    monkeypatch.delenv("JOB_DB", raising=False)
    monkeypatch.setenv("JOB_API_TOKEN", "test-token")
    r = TestClient(jobapi.app).post(f"/jobs/{slug}/read", json={"read": True},
                                    headers=AUTH)
    assert r.status_code == 503


def test_auth_is_checked_before_the_database_is_opened(monkeypatch, slug):
    """_auth is an app-level dependency, so it solves before the route's _db.
    An unauthenticated caller gets 401 and never touches the database, even
    when the database is the thing that is misconfigured. That ordering is the
    security property: it is what keeps 503 from being a probe.
    """
    monkeypatch.delenv("JOB_DB", raising=False)
    monkeypatch.setenv("JOB_API_TOKEN", "test-token")
    client = TestClient(jobapi.app)
    assert client.post(f"/jobs/{slug}/read", json={"read": True}).status_code == 401
    assert client.post(f"/jobs/{slug}/read", json={"read": True},
                       headers=AUTH).status_code == 503


def test_a_missing_api_token_refuses_every_request(monkeypatch, tmp_path, slug):
    """No token configured means no request is authenticated, so the service
    refuses rather than serving writes to anyone who can reach the port."""
    path = tmp_path / "jobs.db"
    jobdb.JobDB(path).close()
    monkeypatch.setenv("JOB_DB", str(path))
    monkeypatch.delenv("JOB_API_TOKEN", raising=False)
    client = TestClient(jobapi.app)
    assert client.post(f"/jobs/{slug}/read", json={"read": True},
                       headers=AUTH).status_code == 503
    assert client.post(f"/jobs/{slug}/read", json={"read": True}).status_code == 503
