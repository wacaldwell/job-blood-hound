"""The semantic screen: a second, one-directional disqualifier pass that catches
requirements meaning a forbidden competency in words the substring ledger has
never seen (the live coding-bar failure mode, generalized). Plus the punctuation and
whitespace robustness added to the substring matcher itself (_norm/_touches).

Offline. The screen's model call is injected, so nothing here hits the network.
"""
import json

import gate
import jobdb


DNC = [
    {"claim": "hand-writing production code",
     "match": ["proficient in python", "python or go"]},
    {"claim": "data catalog architecture", "match": ["data catalog"]},
]


def _req(**kw):
    base = {"quote": "q", "topic": "t", "hard": True, "confidence": "high",
            "verdict": "HAVE", "evidence": "solid evidence", "bridge": "",
            "forced": "", "ruled_by_human": False}
    base.update(kw)
    return base


def _screen_call(disqualified):
    """A fake screen model that disqualifies exactly the given index/claim pairs."""
    def call(system, user, api_key):
        assert system == gate.SCREEN_SYSTEM  # run through the screen, not extract
        return json.dumps({"disqualified": disqualified})
    return call


# --- _norm / _touches: punctuation and whitespace robustness ---------------

def test_touches_matches_across_hyphen_and_collapsed_whitespace():
    entry = {"claim": "data catalog architecture", "match": ["data catalog"]}
    assert gate._touches(_req(quote="Deep expertise in data-catalog architecture"), entry)
    assert gate._touches(_req(quote="build a  data   catalog"), entry)
    assert gate._touches(_req(quote="data\ncatalog design"), entry)


def test_touches_matches_python_or_go_through_a_comma():
    entry = {"claim": "x", "match": ["python or go"]}
    assert gate._touches(_req(quote="Proficient in Python, or Go"), entry)


def test_touches_does_not_fire_when_the_tokens_are_not_adjacent():
    entry = {"claim": "x", "match": ["data catalog"]}
    assert not gate._touches(
        _req(quote="Own the data pipeline and the service catalog"), entry)


# --- match_word: whole-word tokens -----------------------------------------
# For a competency whose name is a short word living inside unrelated ordinary
# words. 'ecs' is inside 'specs' and 'eks' inside 'weeks', so a substring token
# misfires; multi-word forms ('amazon ecs') miss a bare 'ECS' requirement.

def test_match_word_fires_on_a_standalone_token():
    entry = {"claim": "x", "match_word": ["ecs", "eks"]}
    assert gate._touches(_req(quote="5+ years running production on ECS"), entry)
    assert gate._touches(_req(quote="Deep experience with EKS"), entry)


def test_match_word_fires_across_separator_punctuation():
    """_norm collapses the slash, so 'ECS/EKS' is two whole words, not one."""
    entry = {"claim": "x", "match_word": ["ecs"]}
    assert gate._touches(_req(quote="ECS/EKS"), entry)
    assert gate._touches(_req(quote="(ECS, or EKS)"), entry)


def test_match_word_does_not_fire_inside_a_longer_word():
    entry = {"claim": "x", "match_word": ["ecs", "eks"]}
    assert not gate._touches(_req(quote="Write clear technical specs"), entry)
    assert not gate._touches(_req(quote="within the first two weeks"), entry)


def test_match_word_matches_at_either_end_of_the_text():
    """The padding, not a regex, is what makes the ends work."""
    entry = {"claim": "x", "match_word": ["ecs"]}
    assert gate._touches(_req(quote="ECS", topic=""), entry)


def test_match_and_match_word_compose_on_one_entry():
    entry = {"claim": "x", "match": ["kubernetes"], "match_word": ["ecs"]}
    assert gate._touches(_req(quote="Deep Kubernetes experience"), entry)
    assert gate._touches(_req(quote="Own our ECS estate"), entry)
    assert not gate._touches(_req(quote="Own our Docker image standards"), entry)


# --- semantic_screen -------------------------------------------------------

def test_screen_forces_none_on_a_paraphrased_gap_the_ledger_missed():
    # The ledger tokens are 'proficient in python' / 'python or go'. This wording
    # has neither, so enforce() leaves it HAVE. The screen catches the meaning.
    enforced = gate.enforce(
        [_req(quote="You ship production code every week in a compiled language",
              topic="coding", verdict="HAVE", evidence="scripting")], DNC)
    assert enforced[0]["verdict"] == "HAVE", "precondition: ledger must miss this"

    out, info = gate.semantic_screen(
        enforced, DNC, api_key="k",
        call=_screen_call([{"index": 0, "claim": "hand-writing production code"}]))
    assert out[0]["verdict"] == "NONE"
    assert out[0]["forced"] == "semantic-screen: hand-writing production code"
    assert info["forced"] == [{"index": 0, "claim": "hand-writing production code"}]


def test_screen_never_touches_a_none_and_sends_no_none_to_the_model():
    # A NONE is not a candidate, so it is neither screened nor changeable.
    out, info = gate.semantic_screen(
        [_req(verdict="NONE", evidence="")], DNC, api_key="k",
        call=_screen_call([{"index": 0, "claim": "x"}]))
    assert out[0]["verdict"] == "NONE"
    assert info["screened"] == 0


def test_screen_leaves_a_ledger_forced_requirement_alone():
    reqs = gate.enforce(
        [_req(quote="Proficient in Python or Go", verdict="HAVE", evidence="x")], DNC)
    assert reqs[0]["forced"].startswith("do-not-claim")
    out, info = gate.semantic_screen(
        reqs, DNC, api_key="k",
        call=_screen_call([{"index": 0, "claim": "y"}]))
    assert out[0]["forced"].startswith("do-not-claim")  # unchanged
    assert info["screened"] == 0


def test_screen_makes_no_call_when_there_is_nothing_to_screen():
    def boom(system, user, api_key):
        raise AssertionError("the screen must not call the model with no candidates")
    out, info = gate.semantic_screen(
        [_req(verdict="NONE", evidence="")], DNC, api_key="k", call=boom)
    assert info["screened"] == 0
    assert info["forced"] == []


def test_screen_falls_back_unchanged_on_garbage_json():
    reqs = [_req(verdict="HAVE", evidence="e", quote="write code daily", topic="c")]
    out, info = gate.semantic_screen(
        reqs, DNC, api_key="k", call=lambda s, u, k: "sure, looks like a fit!")
    assert out[0]["verdict"] == "HAVE"   # never weaker than the ledger, never a crash
    assert info["error"]


def test_screen_ignores_an_out_of_range_index():
    reqs = [_req(verdict="HAVE", evidence="e", quote="write code daily", topic="c")]
    out, info = gate.semantic_screen(
        reqs, DNC, api_key="k", call=_screen_call([{"index": 9, "claim": "x"}]))
    assert out[0]["verdict"] == "HAVE"
    assert info["forced"] == []


def test_screen_off_switch_disables_it(monkeypatch):
    monkeypatch.setenv("JOB_GATE_SCREEN", "off")
    reqs = [_req(verdict="HAVE", evidence="e", quote="write code daily", topic="c")]
    out, info = gate.semantic_screen(
        reqs, DNC, api_key="k",
        call=_screen_call([{"index": 0, "claim": "x"}]))
    assert out[0]["verdict"] == "HAVE"
    assert info["screened"] == 0


def test_screen_does_not_mutate_its_input():
    reqs = [_req(verdict="HAVE", evidence="e", quote="write code daily", topic="c")]
    gate.semantic_screen(reqs, DNC, api_key="k",
                         call=_screen_call([{"index": 0, "claim": "x"}]))
    assert reqs[0]["verdict"] == "HAVE"  # original list untouched


# --- integration through run_gate ------------------------------------------

MASTER = {
    "works_as": ["manager"],
    "capabilities": [{"claim": "AWS governance", "evidence": "Northwind, 10 accounts"}],
    "do_not_claim": [{"claim": "hand-writing production code",
                      "match": ["proficient in python", "python or go"]}],
}


def _db(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"ats": "greenhouse", "company": "acme", "id": "1",
                   "title": "Engineering Manager", "location": "Remote"})
    return db, db.get(jobdb.make_job_uid("greenhouse", "acme", "1"))


def test_run_gate_screen_catches_a_paraphrased_coding_bar(tmp_path):
    """End to end: the extractor grades a paraphrased coding requirement HAVE, the
    substring ledger misses it, and the screen demotes it, moving the decision from
    a clean PROCEED to CONDITIONAL. This is the regression the screen exists for."""
    def fake_call(system, user, api_key):
        if system == gate.SCREEN_SYSTEM:
            return json.dumps({"disqualified": [
                {"index": 0, "claim": "hand-writing production code"}]})
        return json.dumps({"requirements": [
            {"quote": "You ship production code every week in a compiled language",
             "topic": "coding", "hard": True, "confidence": "high",
             "verdict": "HAVE", "evidence": "scripting", "bridge": ""},
            {"quote": "Lead the operations team", "topic": "leadership",
             "hard": True, "confidence": "high", "verdict": "HAVE",
             "evidence": "led teams at Helio and Northwind", "bridge": ""},
        ]})

    db, row = _db(tmp_path)
    out = gate.run_gate(db, row, MASTER, api_key="k", jd_text="jd", call=fake_call)

    assert out["decision"] == gate.CONDITIONAL
    coding = next(r for r in out["requirements"] if r["topic"] == "coding")
    assert coding["verdict"] == "NONE"
    assert coding["forced"] == "semantic-screen: hand-writing production code"
    assert out["screen"]["forced"] == [
        {"index": 0, "claim": "hand-writing production code"}]

    text = open(out["report_path"]).read()
    assert "semantic screen" in text
    assert "—" not in text and "--" not in text  # project hard rule


def test_run_gate_survives_a_screen_that_errors(tmp_path):
    """A flaky screen must not weaken or crash the gate: the ledger decision stands
    and the report says the screen did not run."""
    def fake_call(system, user, api_key):
        if system == gate.SCREEN_SYSTEM:
            raise RuntimeError("screen provider 500")
        return json.dumps({"requirements": [
            {"quote": "Lead the operations team", "topic": "leadership",
             "hard": True, "confidence": "high", "verdict": "HAVE",
             "evidence": "led teams", "bridge": ""},
        ]})

    db, row = _db(tmp_path)
    out = gate.run_gate(db, row, MASTER, api_key="k", jd_text="jd", call=fake_call)

    assert out["decision"] in (gate.PROCEED, gate.RECOMMEND)  # ledger decision stands
    assert out["screen"]["error"]
    assert "semantic screen" in open(out["report_path"]).read()


# --- review fixes: the four holes Codex found on PR #80 --------------------

def test_touches_keeps_punctuation_that_names_the_competency():
    """Normalization is for separators, not for names. 'C++' must not collapse to
    'c', which is a substring of almost every requirement ever written."""
    entry = {"claim": "C++ development", "match": ["C++"]}
    assert not gate._touches(_req(quote="Demonstrated architecture leadership"), entry)
    assert not gate._touches(_req(quote="Own the incident cadence"), entry)
    assert gate._touches(_req(quote="Ten years of C++ in production"), entry)

    dotnet = {"claim": ".NET", "match": [".NET"]}
    assert not gate._touches(_req(quote="Deep networking background"), dotnet)
    assert gate._touches(_req(quote="Services written in .NET"), dotnet)


def test_touches_ignores_an_empty_match_token():
    """An empty token normalizes to '', which is a substring of everything and
    would force every requirement to NONE without a word in the report."""
    assert not gate._touches(_req(quote="anything at all"),
                             {"claim": "x", "match": ["", "   "]})


def test_touches_separator_normalization_still_works():
    """The fix must not walk back what the PR added: interior punctuation is
    still normalized away."""
    entry = {"claim": "data catalog architecture", "match": ["data-catalog"]}
    assert gate._touches(_req(quote="own the data catalog"), entry)


def test_screen_survives_malformed_hit_fields():
    """The hit loop sits outside the try, so unvalidated fields would crash the
    gate instead of falling back to the ledger. An unhashable index raises on
    `in valid`; a non-string claim raises on .strip()."""
    reqs = [_req(verdict="HAVE", evidence="e"), _req(verdict="HAVE", evidence="e")]
    for hits in ([{"index": []}],
                 [{"index": {"a": 1}, "claim": "x"}],
                 [{"index": "0", "claim": "x"}],
                 [{"index": None}],
                 [{"index": 99, "claim": "x"}]):
        out, info = gate.semantic_screen(reqs, DNC, api_key="k",
                                         call=_screen_call(hits))
        assert info["error"] is None
        assert info["forced"] == []
        assert [r["verdict"] for r in out] == ["HAVE", "HAVE"]


def test_screen_does_not_read_a_boolean_index_as_requirement_one():
    """bool is an int subclass and True == 1, so an unguarded `idx in valid`
    would force requirement 1 on a JSON `true`."""
    reqs = [_req(verdict="HAVE", evidence="e"), _req(verdict="HAVE", evidence="e")]
    out, info = gate.semantic_screen(
        reqs, DNC, api_key="k", call=_screen_call([{"index": True, "claim": "x"}]))
    assert info["forced"] == []
    assert out[1]["verdict"] == "HAVE"


def test_screen_keeps_the_good_hits_when_one_hit_is_malformed():
    """Per-hit validation, not an all-or-nothing try: one bad hit costs that hit."""
    reqs = [_req(verdict="HAVE", evidence="e"), _req(verdict="HAVE", evidence="e")]
    out, info = gate.semantic_screen(
        reqs, DNC, api_key="k",
        call=_screen_call([{"index": []}, {"index": 1, "claim": "coding"}]))
    assert out[0]["verdict"] == "HAVE"
    assert out[1]["verdict"] == "NONE"
    assert info["forced"] == [{"index": 1, "claim": "coding"}]


def test_screen_defaults_a_non_string_claim_rather_than_crashing():
    reqs = [_req(verdict="HAVE", evidence="e")]
    out, info = gate.semantic_screen(
        reqs, DNC, api_key="k", call=_screen_call([{"index": 0, "claim": 1}]))
    assert out[0]["verdict"] == "NONE"
    assert out[0]["forced"] == "semantic-screen: semantic match"


def test_forced_by_ledger_covers_both_match_routes_and_nothing_else():
    assert gate.forced_by_ledger({"forced": "do-not-claim: data catalog"})
    assert gate.forced_by_ledger({"forced": "semantic-screen: coding"})
    # Verdict demotions, not ledger hits: their classification stays adjudicable.
    assert not gate.forced_by_ledger({"forced": "no-evidence"})
    assert not gate.forced_by_ledger({"forced": "no-bridge"})
    assert not gate.forced_by_ledger({"forced": ""})
    assert not gate.forced_by_ledger({})


def test_recompute_preserves_the_screen_record(tmp_path, monkeypatch):
    """recompute() rewrites gate_json wholesale. Dropping 'screen' would erase the
    record that the screen failed, and with it the report's warning that the
    decision rests on the substring ledger alone."""
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))
    db, row = _db(tmp_path)
    screen = {"screened": 2, "forced": [], "error": "screen provider 500"}
    reqs = [_req(quote="q", topic="t", confidence="low", verdict="NONE",
                 evidence="", hard=True)]
    db.set_gate(row["uid"], gate.NEEDS_REVIEW,
                json.dumps({"requirements": reqs, "title": {}, "screen": screen}),
                "/tmp/r.md")

    out = gate.recompute(db, db.get(row["uid"]))

    assert out["screen"] == screen
    assert json.loads(db.get(row["uid"])["gate_json"])["screen"] == screen
    assert "could not run" in open(out["report_path"]).read()
