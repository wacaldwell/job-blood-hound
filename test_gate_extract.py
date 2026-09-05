# test_gate_extract.py
import json
import pytest
import gate

CAPS = [{"claim": "multi-account AWS governance", "evidence": "Northwind, 10 accounts"}]


def test_extract_sends_jd_and_capabilities_and_parses():
    captured = {}

    def fake_call(system, user, api_key):
        captured["system"] = system
        captured["user"] = user
        return json.dumps({"requirements": [
            {"quote": "Proficient in Python or Go", "topic": "python or go",
             "hard": True, "confidence": "high", "verdict": "NONE",
             "evidence": "", "bridge": ""},
        ]})

    reqs = gate.extract({"title": "Senior Staff Engineer", "company": "globex"},
                        "Proficient in Python or Go", CAPS, api_key="k",
                        call=fake_call)
    assert reqs[0]["topic"] == "python or go"
    assert reqs[0]["hard"] is True
    assert "Proficient in Python or Go" in captured["user"]
    assert "multi-account AWS governance" in captured["user"]


def test_extract_prompt_teaches_the_and_or_trap():
    """The single most important thing in the prompt. Pin it."""
    sys_prompt = gate.EXTRACT_SYSTEM.lower()
    assert "and/or" in sys_prompt
    assert "conjunctive" in sys_prompt
    # A conjunctive hard list must SPLIT into one requirement per item.
    assert "split" in sys_prompt
    # Must forbid paraphrasing.
    assert "verbatim" in sys_prompt
    # Must forbid em dashes, per the project hard rule.
    assert "em dash" in sys_prompt


def test_extract_strips_markdown_fences():
    def fake_call(system, user, api_key):
        return '```json\n{"requirements": [{"quote": "q", "topic": "t", "hard": false, "confidence": "high", "verdict": "HAVE", "evidence": "e", "bridge": ""}]}\n```'

    reqs = gate.extract({"title": "t", "company": "c"}, "jd", CAPS,
                        api_key="k", call=fake_call)
    assert reqs[0]["verdict"] == "HAVE"


def test_malformed_json_raises_rather_than_guessing():
    def fake_call(system, user, api_key):
        return "I think this is a great fit for you!"

    with pytest.raises(ValueError):
        gate.extract({"title": "t", "company": "c"}, "jd", CAPS,
                     api_key="k", call=fake_call)


def test_missing_fields_are_defaulted_conservatively():
    """A requirement the model returns half-populated must not silently PASS."""
    def fake_call(system, user, api_key):
        return json.dumps({"requirements": [{"quote": "q", "topic": "t"}]})

    reqs = gate.extract({"title": "t", "company": "c"}, "jd", CAPS,
                        api_key="k", call=fake_call)
    r = reqs[0]
    assert r["verdict"] == "NONE"      # default to the harsh side
    assert r["hard"] is True           # default to the harsh side
    assert r["confidence"] == "low"    # unknown confidence is not high


def test_explicit_null_hard_defaults_to_HARD_not_soft():
    """The fail-closed boundary. A model emitting "hard": null must not be able
    to turn a real disqualifier into a soft nice-to-have. bool(None) is False,
    which is the LENIENT side, so this needs an explicit None check."""
    def fake_call(system, user, api_key):
        return json.dumps({"requirements": [
            {"quote": "q", "topic": "t", "hard": None,
             "confidence": "high", "verdict": "NONE", "evidence": "", "bridge": ""},
        ]})

    reqs = gate.extract({"title": "t", "company": "c"}, "jd", CAPS,
                        api_key="k", call=fake_call)
    assert reqs[0]["hard"] is True
    # And it must still be visible to the decision arithmetic.
    assert gate.decide(reqs) == gate.CONDITIONAL


def test_explicit_false_hard_is_respected():
    """The other half: an explicit false must stay soft. A naive `or True` fix
    would break this, so pin it."""
    def fake_call(system, user, api_key):
        return json.dumps({"requirements": [
            {"quote": "q", "topic": "t", "hard": False,
             "confidence": "high", "verdict": "NONE", "evidence": "", "bridge": ""},
        ]})

    reqs = gate.extract({"title": "t", "company": "c"}, "jd", CAPS,
                        api_key="k", call=fake_call)
    assert reqs[0]["hard"] is False


def test_out_of_range_verdict_is_coerced_to_none():
    def fake_call(system, user, api_key):
        return json.dumps({"requirements": [
            {"quote": "q", "topic": "t", "hard": True, "confidence": "high",
             "verdict": "MAYBE", "evidence": "e", "bridge": ""},
        ]})

    reqs = gate.extract({"title": "t", "company": "c"}, "jd", CAPS,
                        api_key="k", call=fake_call)
    assert reqs[0]["verdict"] == "NONE"


def test_non_list_requirements_raises_valueerror_not_attributeerror():
    def fake_call(system, user, api_key):
        return json.dumps({"requirements": "I could not find any"})

    with pytest.raises(ValueError):
        gate.extract({"title": "t", "company": "c"}, "jd", CAPS,
                     api_key="k", call=fake_call)
