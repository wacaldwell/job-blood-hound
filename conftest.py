"""Test-wide safety net.

Two things the suite must never touch: the user's real application packages,
and the database.

job_generate binds APPLICATIONS_ROOT at import time, and pytest imports test
modules during collection, before any test body runs. So a monkeypatch.setenv
of JOB_APPS_DIR inside a test is always too late: the real path is already
frozen. Without this fixture, running the suite writes real files into the
user's actual ~/job-applications folder, which holds live application packages.

Patch the bound attribute, not just the environment.
"""
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_application_packages(tmp_path, monkeypatch):
    """Point every test's package output at a temp dir it cannot escape."""
    import job_generate
    root = tmp_path / "job-applications"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JOB_APPS_DIR", str(root))
    monkeypatch.setattr(job_generate, "APPLICATIONS_ROOT", root)
    return root


@pytest.fixture(autouse=True)
def _isolate_database(tmp_path, monkeypatch):
    """Keep the suite from creating a second jobs.db.

    There is exactly ONE real jobs.db and it lives on the deployment host. Any code
    path that falls back to a default DB path (job_ingest.main uses
    Path.cwd()/jobs.db, job_cli resolves JOB_DB) would otherwise drop a stray
    jobs.db in the repo root when the suite runs. A second database is the bug
    the single-source-of-truth work already had to clean up once; the test suite
    must not be able to recreate it. See docs/single-source-of-truth.md.
    """
    monkeypatch.setenv("JOB_DB", str(tmp_path / "jobs.db"))


@pytest.fixture(autouse=True)
def _isolate_openjobs_cache(tmp_path, monkeypatch):
    """Keep the suite out of the real open-jobs cache.

    Same reasoning as the two fixtures above. openjobs.cache_dir() falls back to
    the directory holding JOB_DB, and on the host that is ~/job-hound, where the
    real manifest, centroids and downloaded group files live. A test that wrote
    there could poison a live daily run with fixture data.
    """
    monkeypatch.setenv("JOB_OPENJOBS_CACHE", str(tmp_path / "openjobs-cache"))


@pytest.fixture(autouse=True)
def _use_example_config(monkeypatch):
    """Point the suite at the tracked *.example.* templates.

    The four operator data files (master_resume.yaml, profile.yaml,
    companies.yaml, ideal-jd.md) hold one person's career and targets, so they
    are gitignored and absent from a fresh clone. Without this the suite passes
    only on a machine that happens to have them, and 23 tests fail for anyone
    who clones the repo, which is the worst kind of green: it depends on
    untracked local state.

    Pointing at the templates instead means the suite exercises the same
    committed fixtures everywhere, and can never read the maintainer's real
    resume even on a machine where it does exist.
    """
    here = Path(__file__).resolve().parent
    for env, name in (
        ("JOB_MASTER", "master_resume.example.yaml"),
        ("JOB_PROFILE", "profile.example.yaml"),
        ("JOB_CONFIG", "companies.example.yaml"),
        ("JOB_IDEAL_JD", "ideal-jd.example.md"),
    ):
        monkeypatch.setenv(env, str(here / name))
