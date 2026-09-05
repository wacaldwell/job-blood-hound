import pytest

import job_cli
import jobdb


def test_explicit_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_DB", str(tmp_path / "env.db"))
    assert job_cli.resolve_db_path(str(tmp_path / "x.db")) == (tmp_path / "x.db")


def test_env_var_used_when_no_override(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_DB", str(tmp_path / "env.db"))
    assert job_cli.resolve_db_path(None) == (tmp_path / "env.db")


def test_local_jobs_db_used_when_present(tmp_path, monkeypatch):
    monkeypatch.delenv("JOB_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "jobs.db").write_text("")  # presence is enough
    assert job_cli.resolve_db_path(None) == (tmp_path / "jobs.db")


def test_refuses_to_invent_a_database(tmp_path, monkeypatch):
    """The regression: no JOB_DB and no local jobs.db must raise, not resolve.

    The old fallback returned a per-user path and mkdir'd its parent, so the
    first open created a second, divergent database. It ran for nine days on
    the Mac before anyone noticed. See docs/single-source-of-truth.md.
    """
    monkeypatch.delenv("JOB_DB", raising=False)
    monkeypatch.chdir(tmp_path)  # no jobs.db here
    with pytest.raises(jobdb.DBPathError):
        job_cli.resolve_db_path(None)


def test_refusal_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("JOB_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(jobdb.DBPathError):
        jobdb.resolve_db_path()
    assert list(tmp_path.rglob("jobs.db")) == []


def test_refusal_points_at_the_one_database(tmp_path, monkeypatch):
    monkeypatch.delenv("JOB_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(jobdb.DBPathError) as e:
        jobdb.resolve_db_path()
    assert "bin/jh" in str(e.value)
