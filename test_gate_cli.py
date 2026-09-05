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


def test_gate_rule_flips_hard_to_soft_and_recomputes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))
    db, row = _db(tmp_path)
    reqs = [{"quote": "q", "topic": "corporate infra", "hard": True,
             "confidence": "low", "verdict": "NONE", "evidence": "",
             "bridge": "", "forced": "", "ruled_by_human": False}]
    db.set_gate(row["uid"], gate.NEEDS_REVIEW,
                json.dumps({"requirements": reqs, "title": {}}), "/tmp/r.md")

    args = type("A", (), {"ident": row["slug"], "n": 1, "hard": False,
                          "soft": True, "note": "reads as soft to me"})()
    job_cli.cmd_gate_rule(db, args)

    saved = json.loads(db.get(row["uid"])["gate_json"])["requirements"]
    assert saved[0]["hard"] is False
    assert saved[0]["ruled_by_human"] is True
    assert db.get(row["uid"])["gate_decision"] == gate.PROCEED


def test_gate_rule_preserves_the_location_block(tmp_path, monkeypatch):
    """Ruling an unsure item on a NOT_REMOTE job must not drop the location
    overlay. gate_json carries a 'location' key that recompute re-applies; if
    cmd_gate_rule rebuilt the json without it, the job would flip to a pass."""
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))
    db, row = _db(tmp_path)
    reqs = [{"quote": "q", "topic": "t", "hard": True, "confidence": "low",
             "verdict": "NONE", "evidence": "", "bridge": "", "forced": "",
             "ruled_by_human": False}]
    db.set_gate(row["uid"], gate.NOT_REMOTE, json.dumps({
        "requirements": reqs, "title": {},
        "location": {"ok": False, "reason": "Cleveland, on-site"},
        "skills_decision": gate.CONDITIONAL}), "/tmp/r.md")

    args = type("A", (), {"ident": row["slug"], "n": 1, "hard": False,
                          "soft": True, "note": "soft"})()
    job_cli.cmd_gate_rule(db, args)

    # The ruling changed a skills item, but the job is STILL not remote.
    assert db.get(row["uid"])["gate_decision"] == gate.NOT_REMOTE


def test_gate_override_requires_a_reason(tmp_path):
    db, row = _db(tmp_path)
    db.set_gate(row["uid"], gate.DO_NOT_APPLY, "{}", "/tmp/r.md")
    args = type("A", (), {"ident": row["slug"], "reason": "   "})()
    with pytest.raises(SystemExit):
        job_cli.cmd_gate_override(db, args)


def test_gaps_lists_open_gaps(tmp_path, capsys):
    db, row = _db(tmp_path)
    db.add_gap(row["uid"], "Proficient in Python or Go")
    args = type("A", (), {"ident": None})()
    job_cli.cmd_gaps(db, args)
    assert "Python or Go" in capsys.readouterr().out


def test_gap_plan_exits_on_a_bad_id(tmp_path):
    db, row = _db(tmp_path)
    args = type("A", (), {"gap_id": 9999, "plan": "Study K8s", "hours": 4,
                          "deadline": "2026-08-01"})()
    with pytest.raises(SystemExit):
        job_cli.cmd_gap_plan(db, args)


def test_gap_plan_succeeds_on_a_real_id(tmp_path, capsys):
    db, row = _db(tmp_path)
    gid = db.add_gap(row["uid"], "Kubernetes at scale")
    args = type("A", (), {"gap_id": gid, "plan": "Study K8s", "hours": 10,
                          "deadline": "2026-08-01"})()
    job_cli.cmd_gap_plan(db, args)
    assert "planned" in capsys.readouterr().out
    assert db.gaps_for(row["uid"])[0]["plan"] == "Study K8s"


def test_gap_plan_rejects_zero_hours(tmp_path):
    """A zero-hour plan is not a plan. --hours 0 must not satisfy the gate."""
    db, row = _db(tmp_path)
    gid = db.add_gap(row["uid"], "Kubernetes at scale")
    args = type("A", (), {"gap_id": gid, "plan": "lol", "hours": 0,
                          "deadline": "2026-08-01"})()
    with pytest.raises(SystemExit):
        job_cli.cmd_gap_plan(db, args)
    assert db.gaps_for(row["uid"])[0]["plan"] is None


def test_gap_plan_rejects_a_garbage_deadline(tmp_path):
    """'someday' is not a deadline; it must fail to parse as YYYY-MM-DD."""
    db, row = _db(tmp_path)
    gid = db.add_gap(row["uid"], "Kubernetes at scale")
    args = type("A", (), {"gap_id": gid, "plan": "Study K8s", "hours": 10,
                          "deadline": "someday"})()
    with pytest.raises(SystemExit):
        job_cli.cmd_gap_plan(db, args)
    assert db.gaps_for(row["uid"])[0]["plan"] is None


def test_unplanned_gaps_treats_zero_hours_as_unplanned(tmp_path):
    """jobdb.unplanned_gaps must not be satisfied by a zero-hour plan, even if
    it was written directly (bypassing the CLI's own --hours validation)."""
    db, row = _db(tmp_path)
    gid = db.add_gap(row["uid"], "Kubernetes at scale")
    db.plan_gap(gid, "lol", 0, "2026-08-01")
    unplanned = db.unplanned_gaps(row["uid"])
    assert len(unplanned) == 1 and unplanned[0]["id"] == gid


def test_gap_close_exits_on_a_bad_id(tmp_path):
    db, row = _db(tmp_path)
    args = type("A", (), {"gap_id": 9999, "reason": "Studied K8s, comfortable with it now."})()
    with pytest.raises(SystemExit):
        job_cli.cmd_gap_close(db, args)


def test_gap_close_succeeds_on_a_real_id(tmp_path, capsys):
    db, row = _db(tmp_path)
    gid = db.add_gap(row["uid"], "Kubernetes at scale")
    args = type("A", (), {"gap_id": gid, "reason": "Studied K8s, comfortable with it now."})()
    job_cli.cmd_gap_close(db, args)
    assert "closed" in capsys.readouterr().out
    assert db.gaps_for(row["uid"])[0]["status"] == "closed"


def test_gap_close_requires_a_reason(tmp_path):
    db, row = _db(tmp_path)
    gid = db.add_gap(row["uid"], "Kubernetes at scale")
    args = type("A", (), {"gap_id": gid, "reason": "   "})()
    with pytest.raises(SystemExit):
        job_cli.cmd_gap_close(db, args)
    assert db.gaps_for(row["uid"])[0]["status"] == "open"


def _screen_forced_row(db, row, forced):
    reqs = [{"quote": "You ship production code weekly", "topic": "coding",
             "hard": True, "confidence": "low", "verdict": "NONE",
             "evidence": "", "bridge": "", "forced": forced,
             "ruled_by_human": False}]
    db.set_gate(row["uid"], gate.NEEDS_REVIEW,
                json.dumps({"requirements": reqs, "title": {}}), "/tmp/r.md")
    return type("A", (), {"ident": row["slug"], "n": 1, "hard": False,
                          "soft": True, "note": "feels soft to me"})()


def test_gate_rule_refuses_a_semantic_screen_forced_requirement(tmp_path, monkeypatch):
    """The screen is the do_not_claim ledger matched by meaning, so it is as
    absolute as a substring hit. Without this guard the screen's demotion could be
    ruled SOFT and the disqualifier would vanish from the count, which is exactly
    the hole the ledger exists to close."""
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))
    db, row = _db(tmp_path)
    args = _screen_forced_row(db, row, "semantic-screen: hand-writing production code")

    with pytest.raises(SystemExit) as e:
        job_cli.cmd_gate_rule(db, args)
    assert "semantic screen" in str(e.value) and "adjudicable" in str(e.value)

    saved = json.loads(db.get(row["uid"])["gate_json"])["requirements"]
    assert saved[0]["hard"] is True          # untouched
    assert saved[0]["ruled_by_human"] is False


def test_gate_rule_still_refuses_a_substring_ledger_hit(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))
    db, row = _db(tmp_path)
    args = _screen_forced_row(db, row, "do-not-claim: hand-writing production code")
    with pytest.raises(SystemExit):
        job_cli.cmd_gate_rule(db, args)


def test_gate_rule_still_allows_a_no_evidence_requirement(tmp_path, monkeypatch):
    """A 'no-evidence' demotion is not a ledger hit. Its hard-versus-soft
    classification is still the human's call, and blocking it would be a regression."""
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))
    db, row = _db(tmp_path)
    args = _screen_forced_row(db, row, "no-evidence")
    job_cli.cmd_gate_rule(db, args)
    saved = json.loads(db.get(row["uid"])["gate_json"])["requirements"]
    assert saved[0]["hard"] is False
    assert saved[0]["ruled_by_human"] is True
