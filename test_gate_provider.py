import os
import gate
import llm


def test_gate_key_guard_uses_active_provider(monkeypatch, tmp_path):
    # JOB_PROVIDER=kimi with only MOONSHOT_API_KEY set must NOT fail with
    # "ANTHROPIC_API_KEY not set".
    for k in ("JOB_PROVIDER", "ANTHROPIC_API_KEY", "MOONSHOT_API_KEY", "JOB_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("JOB_PROVIDER", "kimi")
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moon")

    from jobdb import JobDB
    db = JobDB(str(tmp_path / "t.db"))
    db.upsert_job({"ats": "manual", "company": "acme", "id": "x1",
                   "title": "Platform Engineer", "location": "Remote", "url": ""})
    row = db.get(db.resolve("acme")["uid"])

    captured = {}
    def fake_call(system, user, api_key):
        captured["called"] = True
        return '{"requirements": []}'

    master = {"capabilities": [], "do_not_claim": []}
    out = gate.run_gate(db, row, master, jd_text="Build platforms on AWS.",
                        call=fake_call)
    # It reached extract (did not short-circuit on a missing anthropic key).
    assert captured.get("called") is True
    assert out["decision"] != gate.ERROR or "not set" not in (out.get("error") or "")


def test_gate_fails_closed_on_unknown_provider(monkeypatch, tmp_path):
    # JOB_PROVIDER set to a typo/unknown value must fail closed to ERROR,
    # never raise an uncaught ValueError out of run_gate.
    for k in ("JOB_PROVIDER", "ANTHROPIC_API_KEY", "MOONSHOT_API_KEY", "JOB_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("JOB_PROVIDER", "krimi")

    from jobdb import JobDB
    db = JobDB(str(tmp_path / "t.db"))
    db.upsert_job({"ats": "manual", "company": "acme", "id": "x2",
                   "title": "Platform Engineer", "location": "Remote", "url": ""})
    row = db.get(db.resolve("acme")["uid"])

    master = {"capabilities": [], "do_not_claim": []}
    out = gate.run_gate(db, row, master, jd_text="Build platforms on AWS.")

    assert out["decision"] == gate.ERROR
    assert "krimi" in out["error"] or "JOB_PROVIDER" in out["error"]


def test_gate_and_screen_have_separate_usage_labels(monkeypatch):
    prov = llm.Provider("anthropic", "https://x", "k", "m", "2023-06-01")
    seen = []

    monkeypatch.setattr(gate.llm, "resolve_provider",
                        lambda component=None:
                        (seen.append(("resolve", component)) or prov))

    def fake_messages(system, user, *, component=None, **kwargs):
        seen.append(("call", component))
        if component == "gate_screen":
            return '{"disqualified": []}'
        return '{"requirements": []}'

    monkeypatch.setattr(gate.llm, "call_messages", fake_messages)
    assert gate._call_anthropic("s", "u", "k") == '{"requirements": []}'
    assert gate._call_screen("s", "u", "k") == '{"disqualified": []}'
    assert seen == [("resolve", "gate"), ("call", "gate"),
                    ("resolve", "gate"), ("call", "gate_screen")]


def test_run_gate_records_the_component_model(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("JOB_GATE_MODEL", "gate-specific-model")
    from jobdb import JobDB
    db = JobDB(str(tmp_path / "t.db"))
    db.upsert_job({"ats": "manual", "company": "acme", "id": "x3",
                   "title": "Platform Engineer", "location": "Remote"})
    row = db.get(db.resolve("acme")["uid"])

    out = gate.run_gate(
        db, row, {"capabilities": [], "do_not_claim": []}, jd_text="AWS",
        call=lambda system, user, api_key: '{"requirements": []}')

    assert out["model"] == "gate-specific-model"
    assert db.get(row["uid"])["gate_model"] == "gate-specific-model"
