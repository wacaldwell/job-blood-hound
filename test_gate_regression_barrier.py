"""The regression barrier.

If the gate does not fail this posting, it is too soft and is not doing its job.
The fixture is modelled on a real posting that cost an interview: 'data catalog
architecture' and 'correlation' were listed as areas of deep expertise, and
'Proficient in Python or Go' as a requirement. All three were visible on day one.

The live test costs one API call and is skipped without a key, so CI stays
green and free. Run it by hand before merging ANY change to the prompt.
"""
import os
from pathlib import Path

import pytest
import yaml

import gate
import jobdb

FIXTURE = Path(__file__).resolve().parent / "tests" / "fixtures" / "aiops-platform-jd.md"
LIVE = pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"),
                          reason="live gate test needs ANTHROPIC_API_KEY")


def _topics(reqs):
    return " | ".join(r["topic"].lower() + " " + r["quote"].lower() for r in reqs)


@LIVE
def test_the_gate_fails_the_aiops_posting(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))
    master = yaml.safe_load(
        (Path(__file__).resolve().parent / "master_resume.example.yaml").read_text())

    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"ats": "greenhouse", "company": "globex", "id": "7644921",
                   "title": "Senior Staff Operations Engineer, AIOps",
                   "location": "Remote"})
    row = db.get(jobdb.make_job_uid("greenhouse", "globex", "7644921"))

    out = gate.run_gate(db, row, master, jd_text=FIXTURE.read_text())
    reqs = out["requirements"]

    # 1. The headline: it must refuse.
    assert out["decision"] == gate.DO_NOT_APPLY, (
        f"gate returned {out['decision']}, so it is too soft. "
        f"Report:\n{out['report_path'].read_text()}")

    # 2. The three hard NONEs that were knowable on day one.
    hard_nones = [r for r in reqs if r["hard"] and r["verdict"] == "NONE"]
    blob = _topics(hard_nones)
    assert "data catalog" in blob, "missed data catalog architecture"
    assert "correlation" in blob, "missed correlation"
    assert ("python" in blob or "go" in blob), "missed Proficient in Python or Go"

    # 3. The and/or trap: the '15+ years across ... and/or ...' bullet is SOFT.
    fifteen = [r for r in reqs if "15+ years" in r["quote"]]
    assert fifteen, "did not extract the 15+ years bullet at all"
    assert not fifteen[0]["hard"], (
        "classified the 'and/or' bullet as HARD. Any ONE item satisfies it. "
        "This is the trap the prompt must teach.")

    # 4. The title check flags Engineer against an ops-leadership profile.
    assert out["title"]["mismatch"], "did not flag the Senior Staff ENGINEER title"
