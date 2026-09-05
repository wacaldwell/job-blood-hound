import json
import fit


def test_verdict_prompt_carries_jd_and_history_and_parses():
    captured = {}

    def fake_call(system, user, api_key):
        captured["system"] = system
        captured["user"] = user
        return json.dumps({
            "llm_fit_score": 78,
            "llm_rationale": "Strong SA fit, light coding bar.",
            "llm_coding_bar": "light",
        })

    job = {"title": "Solutions Architect", "company": "temporal",
           "location": "Remote", "ats": "greenhouse", "ext_id": "9"}
    master = {"contact": {"name": "Jordan Rivers"}, "summary": "SA"}
    history = [
        {"title": "Senior SRE", "company": "globex",
         "decision": "rejected", "reason": "too code-heavy"},
        {"title": "Solutions Architect", "company": "x",
         "decision": "pursued", "reason": ""},
    ]

    result = fit.verdict(
        job, master, history, api_key="k",
        jd_text="Design reference architectures for customers.",
        call=fake_call,
    )

    assert result["llm_fit_score"] == 78
    assert result["llm_coding_bar"] == "light"
    # History and JD must reach the model.
    assert "too code-heavy" in captured["user"]
    assert "reference architectures" in captured["user"]


def test_verdict_strips_markdown_fences():
    def fake_call(system, user, api_key):
        return "```json\n{\"llm_fit_score\": 50, \"llm_rationale\": \"ok\", \"llm_coding_bar\": \"medium\"}\n```"

    result = fit.verdict(
        {"title": "X", "company": "y"}, {"contact": {"name": "A"}}, [],
        api_key="k", jd_text="jd", call=fake_call)
    assert result["llm_fit_score"] == 50


def test_verdict_system_prompt_forbids_em_dash_and_invention():
    captured = {}

    def fake_call(system, user, api_key):
        captured["system"] = system
        return "{\"llm_fit_score\": 1, \"llm_rationale\": \"x\", \"llm_coding_bar\": \"y\"}"

    fit.verdict({"title": "X", "company": "y"}, {"contact": {"name": "A"}}, [],
                api_key="k", jd_text="jd", call=fake_call)
    assert "em dash" in captured["system"].lower()
