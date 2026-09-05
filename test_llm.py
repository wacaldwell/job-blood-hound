import json
import os
import pytest
import llm


def _clear(monkeypatch):
    for k in ("JOB_PROVIDER", "JOB_MODEL", "JOB_LLM_BASE_URL",
              "JOB_LLM_API_KEY", "JOB_LLM_TIMEOUT",
              "JOB_FIT_MODEL", "JOB_GATE_MODEL", "JOB_DRAFT_MODEL",
              "JOB_LLM_USAGE_LOG", "LOG_DIR",
              "ANTHROPIC_API_KEY", "MOONSHOT_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_default_provider_is_anthropic(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    p = llm.resolve_provider()
    assert p.name == "anthropic"
    assert p.base_url == "https://api.anthropic.com"
    assert p.api_key == "sk-ant"
    assert p.model == "claude-opus-4-8"


def test_kimi_provider_uses_moonshot(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JOB_PROVIDER", "kimi")
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moon")
    p = llm.resolve_provider()
    assert p.name == "kimi"
    assert p.base_url == "https://api.moonshot.ai/anthropic"
    assert p.api_key == "sk-moon"
    assert p.model.startswith("kimi")


def test_job_model_overrides_provider_model(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JOB_PROVIDER", "kimi")
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moon")
    monkeypatch.setenv("JOB_MODEL", "kimi-custom")
    assert llm.resolve_provider().model == "kimi-custom"


def test_anthropic_component_models_keep_opus_for_high_value_work(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    assert llm.resolve_provider(component="fit").model == "claude-haiku-4-5"
    assert llm.resolve_provider(component="gate").model == "claude-opus-4-8"
    assert llm.resolve_provider(component="draft").model == "claude-opus-4-8"


def test_component_model_override_wins_over_global_model(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("JOB_MODEL", "global-model")
    monkeypatch.setenv("JOB_FIT_MODEL", "cheap-fit-model")
    assert llm.resolve_provider(component="fit").model == "cheap-fit-model"
    assert llm.resolve_provider(component="gate").model == "global-model"


def test_kimi_keeps_provider_model_without_a_component_override(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JOB_PROVIDER", "kimi")
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moon")
    assert llm.resolve_provider(component="fit").model.startswith("kimi")


def test_per_piece_overrides_win(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JOB_LLM_BASE_URL", "https://proxy.local/")
    monkeypatch.setenv("JOB_LLM_API_KEY", "sk-proxy")
    p = llm.resolve_provider()
    assert p.base_url == "https://proxy.local"   # trailing slash stripped
    assert p.api_key == "sk-proxy"


def test_unknown_provider_raises(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JOB_PROVIDER", "bogus")
    with pytest.raises(ValueError):
        llm.resolve_provider()


def test_call_messages_truncation_raises(monkeypatch):
    prov = llm.Provider("anthropic", "https://x", "k", "m", "2023-06-01")

    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"stop_reason": "max_tokens",
                    "content": [{"type": "text", "text": "partial"}]}

    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: FakeResp())
    with pytest.raises(llm.OutputTruncated) as e:
        llm.call_messages("s", "u", max_tokens=16000, provider=prov,
                          raise_on_truncation=True)
    assert e.value.max_tokens == 16000


def test_call_messages_returns_text(monkeypatch):
    prov = llm.Provider("anthropic", "https://x", "k", "m", "2023-06-01")

    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "hello "},
                                {"type": "text", "text": "world"}]}

    captured = {}
    def fake_post(url, headers, json, timeout):
        captured.update(url=url, headers=headers, body=json)
        return FakeResp()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    out = llm.call_messages("sys", "usr", max_tokens=1000, provider=prov)
    assert out == "hello world"
    assert captured["url"] == "https://x/v1/messages"
    assert captured["headers"]["x-api-key"] == "k"
    assert captured["body"]["model"] == "m"


def test_call_messages_records_component_tokens_and_cost(monkeypatch, tmp_path):
    log = tmp_path / "llm-usage.log"
    monkeypatch.setenv("JOB_LLM_USAGE_LOG", str(log))
    prov = llm.Provider("anthropic", "https://x", "secret-key",
                        "claude-haiku-4-5", "2023-06-01")

    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 2000, "output_tokens": 100}}

    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: FakeResp())
    assert llm.call_messages("s", "u", max_tokens=10, provider=prov,
                             component="fit") == "ok"

    event = json.loads(log.read_text())
    assert event["component"] == "fit"
    assert event["model"] == "claude-haiku-4-5"
    assert event["input_tokens"] == 2000
    assert event["output_tokens"] == 100
    assert event["estimated_cost_usd"] == 0.0025
    assert "secret-key" not in log.read_text()


def test_usage_log_defaults_under_home_logs(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setattr(llm.Path, "home", lambda: tmp_path)
    assert llm._usage_log_path() == tmp_path / "logs" / "job-hound" / "llm-usage.log"


def test_bad_usage_metadata_never_breaks_a_successful_call(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_LLM_USAGE_LOG", str(tmp_path / "usage.log"))
    prov = llm.Provider("anthropic", "https://x", "k",
                        "claude-haiku-4-5", "2023-06-01")

    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": "not-a-number"}}

    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: FakeResp())
    assert llm.call_messages("s", "u", max_tokens=10, provider=prov,
                             component="fit") == "ok"
    assert not (tmp_path / "usage.log").exists()


def test_provider_carries_its_own_timeout(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.delenv("JOB_LLM_TIMEOUT", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    assert llm.resolve_provider().timeout == 180
    # Moonshot is much slower on the gate's extraction, so it gets a longer cap.
    monkeypatch.setenv("JOB_PROVIDER", "kimi")
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moon")
    assert llm.resolve_provider().timeout == 600


def test_job_llm_timeout_overrides_provider(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("JOB_LLM_TIMEOUT", "42")
    assert llm.resolve_provider().timeout == 42


def test_call_messages_defaults_timeout_to_provider(monkeypatch):
    prov = llm.Provider("kimi", "https://x", "k", "m", "2023-06-01", 600)
    seen = {}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "ok"}]}

    def fake_post(url, headers, json, timeout):
        seen["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    llm.call_messages("s", "u", max_tokens=10, provider=prov)
    assert seen["timeout"] == 600            # provider's, not a hardcoded 180
    llm.call_messages("s", "u", max_tokens=10, provider=prov, timeout=5)
    assert seen["timeout"] == 5              # explicit still wins


def test_bad_timeout_names_the_right_env_var(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("JOB_LLM_TIMEOUT", "notanumber")
    with pytest.raises(ValueError) as e:
        llm.resolve_provider()
    # The gate surfaces this string verbatim in the fit report, so it must point
    # at JOB_LLM_TIMEOUT, not at JOB_PROVIDER.
    assert "JOB_LLM_TIMEOUT" in str(e.value)
    assert "notanumber" in str(e.value)
