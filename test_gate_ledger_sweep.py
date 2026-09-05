"""The ledger must fire on the JD itself, not only on extracted requirements.

Live miss that motivated this (2026-08-18, a Senior Manager SRE posting):
the JD said "This is a player-coach role" verbatim, `player-coach` had been a
do_not_claim token since 2026-07-24, and the gate still returned NEEDS_REVIEW.
The extractor turns bulleted requirements into `quote` fields but skips the
role-framing prose, and `_touches` only ever sees `quote` + `topic`, so the
ledger had nothing to match. Three tokens were present in that JD
(player-coach, kinesis, hands-on) and none of them fired.

`ledger_sweep` closes that hole by scanning the raw JD and synthesizing a hard
requirement for any ledger entry the extraction missed entirely.
"""
import gate

DNC = [
    {"claim": "hand-writing production code",
     "match": ["player-coach", "proficient in python"]},
    {"claim": "high-throughput streaming platforms built from the ground up",
     "match": ["kafka", "kinesis"]},
    {"claim": "data catalog architecture", "match": ["data catalog"]},
]


def _req(**kw):
    base = {"quote": "q", "topic": "t", "hard": True, "confidence": "high",
            "verdict": "HAVE", "evidence": "solid evidence", "bridge": "",
            "forced": "", "ruled_by_human": False}
    base.update(kw)
    return base


PLAYER_COACH_JD = (
    "We are seeking a Sr. Manager of Site Reliability Engineering.\n"
    "This is a player-coach role: you are expected to lead by example and "
    "contribute directly to infrastructure design and tooling.\n"
    "Required: 7+ years of experience in SRE, DevOps, or infrastructure.\n"
)


def test_sweep_synthesizes_a_requirement_for_prose_the_extractor_missed():
    """The exact live miss: the token is in the JD, in no requirement."""
    extracted = [_req(quote="7+ years of experience in SRE, DevOps, or infrastructure")]
    extra = gate.ledger_sweep(PLAYER_COACH_JD, DNC, extracted)

    assert len(extra) == 1
    assert extra[0]["topic"] == "hand-writing production code"
    assert extra[0]["hard"] is True
    assert extra[0]["confidence"] == "high"
    assert extra[0]["verdict"] == "NONE"
    # The quote must be the real sentence, so the report is auditable.
    assert "player-coach" in extra[0]["quote"].lower()


def test_sweep_output_is_forced_by_enforce_like_any_other_ledger_hit():
    """The synthetic requirement goes through the normal path, not a side door."""
    extracted = [_req(quote="7+ years of experience in SRE")]
    reqs = gate.enforce(extracted + gate.ledger_sweep(PLAYER_COACH_JD, DNC, extracted), DNC)

    forced = [r for r in reqs if r["forced"]]
    assert len(forced) == 1
    assert forced[0]["forced"] == "do-not-claim: hand-writing production code"
    assert forced[0]["verdict"] == "NONE"


def test_sweep_does_not_duplicate_an_entry_the_extraction_already_caught():
    """No double-counting: two hard NONEs would wrongly reach DO_NOT_APPLY."""
    extracted = [_req(quote="This is a player-coach role", verdict="NONE")]
    assert gate.ledger_sweep(PLAYER_COACH_JD, DNC, extracted) == []


def test_sweep_reports_one_requirement_per_entry_not_per_token():
    jd = "We run Kafka and Kinesis pipelines at scale."
    extra = gate.ledger_sweep(jd, DNC, [])
    assert len(extra) == 1
    assert extra[0]["topic"] == "high-throughput streaming platforms built from the ground up"


def test_sweep_is_silent_when_the_jd_is_clean():
    jd = "Lead the SRE team. Own SLOs. Manage cloud spend."
    assert gate.ledger_sweep(jd, DNC, []) == []


def test_sweep_finds_every_missed_entry():
    jd = PLAYER_COACH_JD + "Our data platform uses Kinesis for streaming.\n"
    extra = gate.ledger_sweep(jd, DNC, [])
    assert {r["topic"] for r in extra} == {
        "hand-writing production code",
        "high-throughput streaming platforms built from the ground up",
    }


def test_two_missed_entries_reach_do_not_apply():
    """Decision-level proof: that JD should never have been reviewable."""
    jd = PLAYER_COACH_JD + "Data Platform: AWS Glue, PySpark, Kinesis.\n"
    extracted = [_req(quote="7+ years of experience in SRE")]
    reqs = gate.enforce(extracted + gate.ledger_sweep(jd, DNC, extracted), DNC)
    assert gate.decide(reqs) == gate.DO_NOT_APPLY


def test_sweep_never_softens_an_existing_verdict():
    """One-directional, like every other rule in the gate."""
    extracted = [_req(quote="Own SLOs", verdict="HAVE")]
    reqs = gate.enforce(extracted + gate.ledger_sweep(PLAYER_COACH_JD, DNC, extracted), DNC)
    assert reqs[0]["verdict"] == "HAVE"
    assert len(reqs) == 2


def test_sweep_tolerates_an_empty_jd():
    assert gate.ledger_sweep("", DNC, []) == []
    assert gate.ledger_sweep(None, DNC, []) == []


# --- the synthetic quote must always contain the token that fired -------------
# Codex review on PR #94 (P2): _ledger_quote could return a quote that does NOT
# contain the matched token, in two ways. The requirement is still a hard NONE
# so the gate stays closed, but enforce() cannot rematch it, `forced` stays
# empty, and the report shows unrelated text with no ledger attribution. A
# blocked draft the operator cannot audit is the failure mode this whole file
# exists to prevent.

LONG = "Filler sentence about cloud reliability work. " * 12  # > 300 chars
# No sentence break and no newline, so this stays ONE chunk past the 300 cap.
LONG_RUN_ON = "cloud reliability and platform engineering work, " * 9


def test_quote_survives_a_token_split_across_a_newline():
    """_norm collapses the newline so the JD matches, but chunking splits it."""
    jd = LONG + "We need deep data\ncatalog architecture ownership."
    extra = gate.ledger_sweep(jd, DNC, [])
    assert len(extra) == 1
    assert gate._matches_text(extra[0]["quote"], DNC[2]), extra[0]["quote"]


def test_quote_survives_a_token_past_the_length_cap():
    """A single long line whose token sits beyond the 300 char window."""
    jd = LONG_RUN_ON + "and this is a player-coach role for the right person."
    extra = gate.ledger_sweep(jd, DNC, [])
    assert len(extra) == 1
    assert "player-coach" in extra[0]["quote"].lower(), extra[0]["quote"]


def test_ledger_attribution_survives_both_cases():
    """The end-to-end consequence: enforce() must still stamp `forced`."""
    for jd in (LONG + "We need deep data\ncatalog architecture ownership.",
               LONG_RUN_ON + "and this is a player-coach role."):
        reqs = gate.enforce(gate.ledger_sweep(jd, DNC, []), DNC)
        assert len(reqs) == 1
        assert reqs[0]["forced"].startswith("do-not-claim: "), reqs[0]


def test_quote_is_capped_even_when_centred_on_a_late_match():
    jd = LONG_RUN_ON + "and this is a player-coach role for the right person."
    extra = gate.ledger_sweep(jd, DNC, [])
    assert len(extra[0]["quote"]) <= 320  # 300 plus the truncation marker


def test_short_clean_sentence_is_still_quoted_whole():
    """Regression: the common case must keep returning a readable sentence."""
    extra = gate.ledger_sweep(PLAYER_COACH_JD, DNC, [])
    assert extra[0]["quote"].startswith("This is a player-coach role")
    assert "..." not in extra[0]["quote"]


# --- match_word entries must get a real quote too -----------------------------
# #89 added `match_word` (whole-word tokens for 'ecs'/'eks', which sit inside
# 'specs' and 'weeks') and #94 added the raw-JD sweep. They landed separately,
# so _match_span only ever learned about `match`. An entry that fires ONLY via
# match_word therefore matched but had no span, fell back to the head of the JD,
# and lost its ledger attribution: the same auditability bug Codex caught, via a
# different door.

WORD_DNC = [{"claim": "container orchestration platforms",
             "match": ["kubernetes"], "match_word": ["ecs", "eks"]}]


def test_match_word_entry_fires_on_the_raw_jd():
    jd = LONG + "The platform runs on EKS across three regions."
    extra = gate.ledger_sweep(jd, WORD_DNC, [])
    assert len(extra) == 1
    assert extra[0]["topic"] == "container orchestration platforms"


def test_match_word_quote_contains_the_token_that_fired():
    """Run-on line, so the chunk scan cannot rescue it and the span must work."""
    jd = LONG_RUN_ON + "and the whole platform runs on EKS across three regions"
    extra = gate.ledger_sweep(jd, WORD_DNC, [])
    assert "eks" in extra[0]["quote"].lower(), extra[0]["quote"]


def test_match_word_attribution_survives_enforce():
    jd = LONG_RUN_ON + "and the whole platform runs on EKS across three regions"
    reqs = gate.enforce(gate.ledger_sweep(jd, WORD_DNC, []), WORD_DNC)
    assert reqs[0]["forced"] == "do-not-claim: container orchestration platforms"


def test_match_word_still_respects_whole_word_boundaries():
    """The reason match_word exists: 'specs' and 'weeks' must not fire."""
    jd = "Review the specs over the next few weeks with the team."
    assert gate.ledger_sweep(jd, WORD_DNC, []) == []
