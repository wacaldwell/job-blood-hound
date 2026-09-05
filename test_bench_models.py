import bench_models
import llm


def test_evaluate_job_is_side_effect_free(monkeypatch, tmp_path):
    from jobdb import JobDB
    db = JobDB(str(tmp_path / "t.db"))
    db.upsert_job({"ats": "manual", "company": "acme", "id": "x1",
                   "title": "Platform Engineer", "location": "Remote", "url": ""})
    uid = db.resolve("acme")["uid"]
    row = db.get(uid)

    def fake_call(system, user, api_key):
        return '{"requirements": []}'

    prov = type("P", (), {"model": "fake-model", "name": "anthropic",
                          "api_key": "k", "base_url": "https://x",
                          "version": "2023-06-01"})()
    master = {"capabilities": [], "do_not_claim": []}
    res = bench_models.evaluate_job(row, "Build platforms on AWS.", master,
                                    prov, call=fake_call)
    assert "decision" in res
    # No gate row was written: evaluate_job must not persist.
    assert db.get(uid)["gate_decision"] is None


def test_evaluate_job_labels_benchmark_usage(monkeypatch, tmp_path):
    from jobdb import JobDB
    db = JobDB(str(tmp_path / "t.db"))
    db.upsert_job({"ats": "manual", "company": "acme", "id": "x2",
                   "title": "Platform Engineer", "location": "Remote"})
    row = db.get(db.resolve("acme")["uid"])
    provider = llm.Provider("anthropic", "https://x", "k", "model",
                            "2023-06-01")
    seen = {}

    def fake_messages(system, user, **kwargs):
        seen["component"] = kwargs.get("component")
        return '{"requirements": []}'

    monkeypatch.setattr(bench_models.llm, "call_messages", fake_messages)
    bench_models.evaluate_job(
        row, "Build platforms on AWS.",
        {"capabilities": [], "do_not_claim": []}, provider)
    assert seen["component"] == "benchmark"


def test_run_bench_isolates_model_failure(monkeypatch, tmp_path):
    from jobdb import JobDB
    db = JobDB(str(tmp_path / "t.db"))
    db.upsert_job({"ats": "manual", "company": "acme", "id": "x1",
                   "title": "Platform Engineer", "location": "Remote", "url": ""})
    uid = db.resolve("acme")["uid"]
    db.set_fields(uid, description="Build platforms on AWS.")
    db.set_gate(uid, "PROCEED", "{}", "/tmp/r.md")

    def fake_evaluate(job_row, jd_text, master, provider, call=None):
        if provider.model == "model_bad":
            raise RuntimeError("boom")
        return {"decision": "PROCEED"}

    monkeypatch.setattr(bench_models, "evaluate_job", fake_evaluate)
    master = {"capabilities": [], "do_not_claim": []}
    results = bench_models.run_bench(db, [], ["model_ok", "model_bad"], master)

    # One model raising must not sink the run or the other model's result.
    assert len(results) == 1
    decisions = results[0]["decisions"]
    assert decisions["model_ok"] == "PROCEED"
    assert decisions["model_bad"].startswith("ERROR")


def test_render_report_flags_disagreements():
    results = [
        {"slug": "a", "title": "SRE", "decisions": {"opus": "PROCEED", "kimi": "PROCEED"}},
        {"slug": "b", "title": "PE", "decisions": {"opus": "PROCEED", "kimi": "DO_NOT_APPLY"}},
    ]
    md = bench_models.render_report(results)
    assert "Disagreements" in md
    assert "b" in md.split("Disagreements", 1)[1].split("\n\n", 1)[0]
