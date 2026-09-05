import json
import gate
import jobdb

MASTER = {
    "works_as": ["manager", "architect"],
    "capabilities": [{"claim": "AWS governance", "evidence": "Northwind, 10 accounts"}],
    "do_not_claim": [{"claim": "event correlation", "match": ["correlation"]}],
}


def _db(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"ats": "greenhouse", "company": "globex", "id": "1",
                   "title": "Senior Staff Operations Engineer", "location": "Remote"})
    return db, db.get(jobdb.make_job_uid("greenhouse", "globex", "1"))


def test_run_gate_blocks_and_writes_a_report(tmp_path):
    def fake_call(system, user, api_key):
        return json.dumps({"requirements": [
            {"quote": "Deep expertise in correlation", "topic": "correlation",
             "hard": True, "confidence": "high", "verdict": "HAVE",
             "evidence": "I have done some", "bridge": ""},
            {"quote": "Proficient in Python or Go", "topic": "python or go",
             "hard": True, "confidence": "high", "verdict": "NONE",
             "evidence": "", "bridge": ""},
        ]})

    db, row = _db(tmp_path)
    out = gate.run_gate(db, row, MASTER, api_key="k", jd_text="jd", call=fake_call)

    # do_not_claim forced the first one to NONE despite a confident HAVE.
    assert out["decision"] == gate.DO_NOT_APPLY
    assert out["counts"]["known_hard_none"] == 2

    # Persisted.
    saved = db.get(row["uid"])
    assert saved["gate_decision"] == gate.DO_NOT_APPLY

    # Report artifact written, and readable.
    text = open(out["report_path"]).read()
    assert "DO NOT APPLY" in text
    assert "Proficient in Python or Go" in text   # verbatim quote survives
    assert "—" not in text and "--" not in text   # project hard rule

    # Every hard NONE became a tracked gap, not a note.
    gaps = db.gaps_for(row["uid"])
    assert len(gaps) == 2
    assert db.unplanned_gaps(row["uid"]), "fresh gaps start unplanned"


def test_run_gate_fails_closed_when_the_model_returns_garbage(tmp_path):
    def fake_call(system, user, api_key):
        return "sure, looks like a great fit!"

    db, row = _db(tmp_path)
    out = gate.run_gate(db, row, MASTER, api_key="k", jd_text="jd", call=fake_call)
    assert out["decision"] == gate.ERROR
    assert db.get(row["uid"])["gate_decision"] == gate.ERROR


def test_run_gate_fails_closed_with_no_jd(tmp_path):
    """An empty JD must fail closed. The fake response here is well formed and
    would otherwise PROCEED, so this assertion can only pass if the empty-JD
    guard actually fires. (The old version of this test used a malformed
    response, so it passed even with the guard deleted.)"""
    def fake_call(system, user, api_key):
        return json.dumps({"requirements": [
            {"quote": "AWS", "topic": "aws", "hard": True, "confidence": "high",
             "verdict": "HAVE", "evidence": "Northwind, 10 accounts", "bridge": ""},
        ]})

    db, row = _db(tmp_path)
    out = gate.run_gate(db, row, MASTER, api_key="k", jd_text="", call=fake_call)
    assert out["decision"] == gate.ERROR
    assert db.get(row["uid"])["gate_decision"] == gate.ERROR


def test_run_gate_fails_closed_with_no_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db, row = _db(tmp_path)
    out = gate.run_gate(db, row, MASTER, api_key=None, jd_text="a real jd",
                        call=lambda s, u, k: "{}")
    assert out["decision"] == gate.ERROR
    assert db.get(row["uid"])["gate_decision"] == gate.ERROR


def test_run_gate_fails_closed_when_the_jd_fetch_raises(tmp_path):
    def boom(job_row):
        raise RuntimeError("greenhouse 500")

    db, row = _db(tmp_path)
    out = gate.run_gate(db, row, MASTER, api_key="k", fetch_jd=boom,
                        call=lambda s, u, k: "{}")
    assert out["decision"] == gate.ERROR
    assert db.get(row["uid"])["gate_decision"] == gate.ERROR


def test_run_gate_fails_closed_on_a_malformed_capability_ledger(tmp_path):
    """A capability with no evidence is not a capability. A malformed ledger must
    block, not explode, so the job is recorded as blocked rather than silently
    left ungated."""
    bad_master = {"works_as": ["manager"],
                  "capabilities": [{"claim": "quantum computing", "evidence": ""}],
                  "do_not_claim": []}
    db, row = _db(tmp_path)
    out = gate.run_gate(db, row, bad_master, api_key="k", jd_text="jd",
                        call=lambda s, u, k: "{}")
    assert out["decision"] == gate.ERROR
    assert db.get(row["uid"])["gate_decision"] == gate.ERROR


def test_recompute_applies_a_human_ruling_with_no_api_call(tmp_path):
    def fake_call(system, user, api_key):
        return json.dumps({"requirements": [
            {"quote": "Solid understanding of corporate infrastructure",
             "topic": "corporate infra", "hard": True, "confidence": "low",
             "verdict": "NONE", "evidence": "", "bridge": ""},
        ]})

    db, row = _db(tmp_path)
    out = gate.run_gate(db, row, MASTER, api_key="k", jd_text="jd", call=fake_call)
    assert out["decision"] == gate.NEEDS_REVIEW

    # The human rules it SOFT. No API key needed, and none is passed.
    reqs = json.loads(db.get(row["uid"])["gate_json"])["requirements"]
    reqs[0]["hard"] = False
    reqs[0]["ruled_by_human"] = True
    db.set_gate(row["uid"], gate.NEEDS_REVIEW,
                json.dumps({"requirements": reqs}), "/tmp/r.md")

    out2 = gate.recompute(db, db.get(row["uid"]))
    assert out2["decision"] == gate.PROCEED


# --- a wrong-shaped master must fail CLOSED, not raise ---------------------
#
# title_check() runs before the profile is validated and calls master.get(),
# so an emptied master resume (parses as None) or one turned into a list
# by a stray leading dash raised AttributeError straight out of run_gate. No
# ERROR was recorded, the job kept whatever decision it last had, and
# require_pass() would draft a stale PROCEED against a corrupt ledger. A gate
# that fails open is not a gate.

def _malformed(tmp_path, master, ident):
    db = jobdb.JobDB(tmp_path / f"{ident}.db")
    db.upsert_job({"ats": "greenhouse", "company": "acme", "id": ident,
                   "title": "Senior Staff Operations Engineer",
                   "location": "Remote"})
    uid = jobdb.make_job_uid("greenhouse", "acme", ident)
    db.set_gate(uid, gate.PROCEED, json.dumps({"requirements": []}), "old.md")
    out = gate.run_gate(db, db.get(uid), master, api_key="k", jd_text="jd",
                        call=lambda *a, **k: "{}")
    return db, uid, out


def test_a_wrong_shaped_master_records_error_and_clears_a_stale_pass(tmp_path):
    for i, master in enumerate((None, [{"claim": "x"}], "capabilities",
                                {"do_not_claim": ["data catalog"]})):
        db, uid, out = _malformed(tmp_path, master, f"m{i}")
        assert out["decision"] == gate.ERROR, master
        assert db.get(uid)["gate_decision"] == gate.ERROR, master
        db.close()


def test_a_wrong_shaped_master_still_blocks_drafting(tmp_path):
    db, uid, _ = _malformed(tmp_path, None, "block")
    try:
        gate.require_pass(db, db.get(uid))
        raise AssertionError("require_pass let a corrupt-ledger job through")
    except gate.GateBlocked:
        pass
    db.close()


def test_a_wrong_shaped_works_as_also_fails_closed(tmp_path):
    """Same failure class as the malformed-ledger P1, reached through the key
    title_check reads. `works_as: 5` raises TypeError and `works_as: [2026]`
    raises AttributeError, and title_check runs outside the try that guards
    extract(), so both escaped run_gate past _fail."""
    for i, bad in enumerate(({"works_as": 5}, {"works_as": [2026]},
                             {"works_as": "manager"})):
        master = dict(bad, capabilities=[], do_not_claim=[])
        db, uid, out = _malformed(tmp_path, master, f"w{i}")
        assert out["decision"] == gate.ERROR, bad
        assert db.get(uid)["gate_decision"] == gate.ERROR, bad
        db.close()
