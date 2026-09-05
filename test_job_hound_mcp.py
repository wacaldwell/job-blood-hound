"""Tests for the agent-facing MCP adapter (job_hound_mcp).

The tools open the DB via jc.resolve_db_path(None), which honors JOB_DB, so
each test points JOB_DB at a temp file and seeds it through a plain JobDB.
"""

import gate
import jobdb
import job_cli as jc
import job_hound_mcp as mcp


def _seed(db, ext, title, **fields):
    db.upsert_job({"id": ext, "ats": "greenhouse", "company": "acme",
                   "title": title, "location": "Remote", "url": "http://x"})
    uid = jobdb.make_job_uid("greenhouse", "acme", ext)
    if fields:
        db.set_fields(uid, **fields)
    return uid


def _use_db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("JOB_DB", str(path))
    return jobdb.JobDB(path)


def test_no_submit_tool_exists_and_apply_is_a_stamp(tmp_path, monkeypatch):
    # The hard rule, asserted: nothing in the surface submits/applies/logs in.
    names = {fn.__name__ for fn in mcp.TOOLS}
    for forbidden in ("submit", "fill", "login", "send"):
        assert not any(forbidden in n for n in names), names
    # An override is a deliberate human act and stays on the CLI, not the agent.
    for forbidden in ("override", "rule"):
        assert not any(forbidden in n for n in names), names

    db = _use_db(tmp_path, monkeypatch)
    uid = _seed(db, "1", "Senior SRE")
    db.set_state(uid, "queued"); db.set_state(uid, "drafted")
    db.set_state(uid, "ready")
    db.close()

    res = mcp.job_apply("acme")
    assert res["ok"] and res["to"] == "applied"
    # It only changed state; it did not (and cannot) reach a job site.
    check = jobdb.JobDB(tmp_path / "t.db")
    assert check.get(uid)["state"] == "applied"
    check.close()


def test_stats_and_list(tmp_path, monkeypatch):
    db = _use_db(tmp_path, monkeypatch)
    _seed(db, "1", "Staff Solutions Architect")
    _seed(db, "2", "Principal Architect")
    db.close()

    stats = mcp.job_stats()
    assert stats["total"] == 2
    assert stats["by_state"]["discovered"] == 2

    listed = mcp.job_list(include_all=True)
    assert listed["shown"] == 2
    job = listed["jobs"][0]
    assert {"slug", "title", "company", "state", "age", "url"} <= set(job)


def _commit_with_ancient_posting(db, uid):
    """Push one seeded lead to `drafted` with a 40-day-old posting."""
    db.conn.execute(
        "UPDATE jobs SET posted_at = datetime('now', '-40 days'),"
        " date_source = 'greenhouse:first_published' WHERE uid = ?", (uid,))
    db.conn.commit()
    db.set_state(uid, "queued")
    db.set_state(uid, "drafted")


def test_list_keeps_a_committed_lead_with_an_ancient_posting(tmp_path, monkeypatch):
    """The default (no include_all) path, which is the one the agent actually
    calls. Without the committed exemption in jc._fresh_filter this lead is
    40 days past the 30d window and silently disappears, which is the whole
    reason the exemption sits there rather than in cmd_list."""
    db = _use_db(tmp_path, monkeypatch)
    uid = _seed(db, "1", "Principal SRE")
    _commit_with_ancient_posting(db, uid)
    db.close()

    listed = mcp.job_list()
    assert [j["title"] for j in listed["jobs"]] == ["Principal SRE"]
    assert listed["hidden_by_age"] == 0


def test_list_limit_is_a_budget_on_discoveries_only(tmp_path, monkeypatch):
    """`limit` bounds the discovery firehose, not the committed set: an agent
    passing limit=1 to bound its response must still see everything the human
    has committed to. Matches the lead inbox's applyRowLimit."""
    db = _use_db(tmp_path, monkeypatch)
    committed = _seed(db, "1", "Principal SRE")
    _commit_with_ancient_posting(db, committed)
    _seed(db, "2", "Platform Engineer")
    _seed(db, "3", "Cloud Architect")
    db.close()

    listed = mcp.job_list(limit=1)
    states = [j["state"] for j in listed["jobs"]]
    # 1 discovery (the whole budget) plus the committed lead, which is free.
    assert sorted(states) == ["discovered", "drafted"]
    assert listed["shown"] == 2


def test_show_resolves_and_reports_missing(tmp_path, monkeypatch):
    db = _use_db(tmp_path, monkeypatch)
    _seed(db, "1", "Senior SRE")
    db.close()

    assert mcp.job_show("does-not-exist")["error"]
    detail = mcp.job_show("acme")
    assert detail["title"] == "Senior SRE"
    assert detail["state"] == "discovered"
    assert detail["history"][0]["to"] == "discovered"


def test_lifecycle_transition_and_illegal_jump(tmp_path, monkeypatch):
    db = _use_db(tmp_path, monkeypatch)
    _seed(db, "1", "Senior SRE")
    db.close()

    ok = mcp.job_queue("acme", note="pursuing")
    assert ok["ok"] and ok["from"] == "discovered" and ok["to"] == "queued"

    # queued -> applied is not a legal jump; the surface refuses it cleanly.
    bad = mcp.job_apply("acme")
    assert "error" in bad and "illegal" in bad["error"]


def test_queue_runs_the_gate_and_returns_decision(tmp_path, monkeypatch):
    """The agent/Discord fix: job_queue must run the gate exactly like
    job_cli.cmd_queue does, so a job queued from Discord is not left with
    gate_decision NULL (which permanently blocks job_draft with no way to
    unbreak it from Discord)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db = _use_db(tmp_path, monkeypatch)
    uid = _seed(db, "1", "Senior SRE")
    db.close()

    res = mcp.job_queue("acme")
    assert res["ok"] and res["to"] == "queued"
    # No API key in this test env, so the gate fails closed to ERROR rather
    # than crashing; the point is that it ran at all and reported a decision.
    assert res["gate_decision"] == gate.ERROR
    assert "gate_counts" in res
    assert res["gate_report_path"]

    check = jobdb.JobDB(tmp_path / "t.db")
    assert check.get(uid)["gate_decision"] == gate.ERROR
    check.close()


def test_job_gate_tool_runs_gate_and_returns_report(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db = _use_db(tmp_path, monkeypatch)
    _seed(db, "1", "Senior SRE")
    db.close()

    res = mcp.job_gate("acme")
    assert res["slug"].startswith("acme__senior-sre__")
    assert res["decision"] == gate.ERROR  # no API key set in this test
    assert "counts" in res
    assert res["report_path"]


def test_job_gate_reports_missing_job(tmp_path, monkeypatch):
    db = _use_db(tmp_path, monkeypatch)
    db.close()
    assert mcp.job_gate("does-not-exist")["error"]


def test_skip_stores_reason(tmp_path, monkeypatch):
    db = _use_db(tmp_path, monkeypatch)
    uid = _seed(db, "1", "Senior SRE")
    db.close()

    res = mcp.job_skip("acme", reason="too code-heavy")
    assert res["ok"] and res["to"] == "skipped"
    check = jobdb.JobDB(tmp_path / "t.db")
    assert check.get(uid)["skip_reason"] == "too code-heavy"
    check.close()


def test_close_requires_valid_outcome(tmp_path, monkeypatch):
    db = _use_db(tmp_path, monkeypatch)
    uid = _seed(db, "1", "Senior SRE")
    for s in ("queued", "drafted", "ready", "applied"):
        db.set_state(uid, s)
    db.close()

    assert mcp.job_close("acme", outcome="banana")["error"]
    res = mcp.job_close("acme", outcome="rejected", reason="no response")
    assert res["ok"] and res["to"] == "closed"
    check = jobdb.JobDB(tmp_path / "t.db")
    assert check.get(uid)["close_reason"] == "no response"
    check.close()


def test_draft_refuses_undrafted_state_without_calling_model(tmp_path, monkeypatch):
    # A 'discovered' job must be queued first; the guard returns before any
    # network/model call, so this needs no API key.
    db = _use_db(tmp_path, monkeypatch)
    _seed(db, "1", "Senior SRE")
    db.close()

    res = mcp.job_draft("acme")
    assert "error" in res and "queue" in res["error"].lower()


def test_draft_returns_package_shape(tmp_path, monkeypatch):
    db = _use_db(tmp_path, monkeypatch)
    uid = _seed(db, "1", "Senior SRE")
    db.set_state(uid, "queued")
    db.close()

    def fake_generate(db_, row, master_path=None, api_key=None):
        return {"version": 1, "folder": "/apps/acme", "pdf": False,
                "files": {"resume": "/apps/acme/r.docx"},
                "tailoring_note": "Emphasized AI ops; gap: no k8s."}

    import job_generate
    monkeypatch.setattr(job_generate, "generate", fake_generate)

    res = mcp.job_draft("acme")
    assert res["ok"] and res["version"] == 1 and res["state"] == "drafted"
    assert res["files"]["resume"].endswith("r.docx")
    assert "gap" in res["tailoring_note"]
    check = jobdb.JobDB(tmp_path / "t.db")
    assert check.get(uid)["state"] == "drafted"
    check.close()


def test_draft_is_gated_and_does_not_monkeypatch_generate(tmp_path, monkeypatch):
    """Tripwire for the agent/Discord path: this test does NOT monkeypatch
    job_generate.generate, so it exercises the real gate.require_pass guard
    that lives at the top of generate(). An ungated fixture job must be
    refused, not drafted, proving the MCP tool cannot be used to bypass the
    fit gate."""
    db = _use_db(tmp_path, monkeypatch)
    uid = _seed(db, "1", "Senior SRE")
    db.set_state(uid, "queued")
    db.close()

    res = mcp.job_draft("acme")
    assert "error" in res
    assert "ok" not in res
    assert "not been gated" in res["error"], (
        f"expected the fit gate to be what blocked this, got: {res['error']!r}. "
        "If this is an API key error, the gate did not fire and this test is vacuous.")

    check = jobdb.JobDB(tmp_path / "t.db")
    assert check.get(uid)["state"] == "queued"  # never advanced to drafted
    check.close()


def test_refine_returns_digest_text(tmp_path, monkeypatch):
    db = _use_db(tmp_path, monkeypatch)
    _seed(db, "1", "Staff Solutions Architect")
    db.close()

    res = mcp.job_refine(no_llm=True, include_all=True)
    assert res["digest"] and isinstance(res["digest"], str)
    assert res["llm_used"] is False


def test_refine_default_uses_shared_three_call_cap():
    import inspect
    assert (inspect.signature(mcp.job_refine)
            .parameters["top"].default) == jc.DEFAULT_LLM_TOP == 3


def test_scan_summary_shape(tmp_path, monkeypatch):
    db = _use_db(tmp_path, monkeypatch)
    db.close()

    def fake_scan_and_ingest(db_, cfg, verbose=False):
        return {"matches": 3, "added": 2, "upgraded": 1,
                "manual": [{"name": "Foo", "ats": "workday",
                            "careers_url": "http://foo/careers"}]}

    monkeypatch.setattr(jc, "load_cfg", lambda p: {})
    monkeypatch.setattr(jc, "scan_and_ingest", fake_scan_and_ingest)

    res = mcp.job_scan()
    assert res["matches"] == 3 and res["added"] == 2 and res["dates_upgraded"] == 1
    assert res["manual"][0]["name"] == "Foo"


def test_scan_handles_missing_config_without_crashing(tmp_path, monkeypatch):
    # load_cfg calls sys.exit on a missing config (fine for the CLI, fatal for
    # a long-running server). job_scan must turn that into a clean error dict.
    db = _use_db(tmp_path, monkeypatch)
    db.close()

    def exiting_load_cfg(path):
        raise SystemExit(f"Config not found: {path}")

    monkeypatch.setattr(jc, "load_cfg", exiting_load_cfg)
    res = mcp.job_scan()
    assert "error" in res and "not found" in res["error"].lower()


def test_refine_handles_missing_master(tmp_path, monkeypatch):
    db = _use_db(tmp_path, monkeypatch)
    _seed(db, "1", "Senior SRE")
    db.close()
    monkeypatch.setenv("JOB_MASTER", str(tmp_path / "nope.yaml"))

    res = mcp.job_refine(no_llm=True, include_all=True)
    assert "error" in res and "missing" in res["error"].lower()


def test_build_server_registers_every_tool(monkeypatch):
    # Skips only where the MCP SDK is absent entirely (a dev machine that
    # never installed it). Deliberately does NOT probe
    # `mcp.server.fastmcp`: `mcp` is a required dependency, so an installed
    # but incompatible SDK is a broken production import, not a reason to
    # skip. This test going red is how the mcp 2.0 break was found at all,
    # and probing the submodule would have turned that into a green suite
    # over a server that cannot start.
    import pytest
    pytest.importorskip("mcp")
    server = mcp.build_server()
    assert server is not None
