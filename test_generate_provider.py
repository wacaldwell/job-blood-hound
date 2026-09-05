import job_generate
import llm


def test_generate_call_uses_llm(monkeypatch):
    prov = llm.Provider("anthropic", "https://x", "k", "m", "2023-06-01")
    seen = {}

    def fake_call(system, user, *, max_tokens, provider=None, **kw):
        seen["max_tokens"] = max_tokens
        seen["component"] = kw.get("component")
        return '{"summary": "ok"}'

    monkeypatch.setattr(job_generate.llm, "call_messages", fake_call)
    monkeypatch.setattr(job_generate.llm, "resolve_provider",
                        lambda component=None: (seen.update(resolve=component) or prov))
    out = job_generate._call_model("SYS", "USER", "sk-ant")
    assert out == {"summary": "ok"}
    assert seen["max_tokens"] == 4000
    assert seen["resolve"] == "draft"
    assert seen["component"] == "draft"
