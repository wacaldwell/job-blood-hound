import gate


def _req(hard=True, confidence="high", verdict="HAVE", ruled=False):
    return {"quote": "q", "topic": "t", "hard": hard, "confidence": confidence,
            "verdict": verdict, "evidence": "e", "bridge": "", "forced": "",
            "ruled_by_human": ruled}


def test_no_hard_none_proceeds():
    assert gate.decide([_req(verdict="HAVE"), _req(hard=False, verdict="NONE")]) == gate.PROCEED


def test_one_hard_none_is_conditional():
    assert gate.decide([_req(verdict="NONE"), _req(verdict="HAVE")]) == gate.CONDITIONAL


def test_two_hard_none_is_do_not_apply():
    assert gate.decide([_req(verdict="NONE"), _req(verdict="NONE")]) == gate.DO_NOT_APPLY


def test_soft_none_does_not_count():
    """A soft requirement the candidate lacks is not disqualifying."""
    reqs = [_req(hard=False, verdict="NONE"), _req(hard=False, verdict="NONE")]
    assert gate.decide(reqs) == gate.PROCEED


def test_low_confidence_none_blocks_as_needs_review():
    reqs = [_req(confidence="low", verdict="NONE"), _req(verdict="HAVE")]
    assert gate.decide(reqs) == gate.NEEDS_REVIEW


def test_low_confidence_on_something_he_has_is_not_unresolved():
    """If he HAS it, hard-vs-soft does not change the outcome, so do not ask."""
    reqs = [_req(confidence="low", verdict="HAVE"), _req(verdict="HAVE")]
    assert gate.decide(reqs) == gate.PROCEED


def test_two_known_hard_none_beats_pending_review():
    """Adjudication cannot rescue an already-doomed posting, so do not ask."""
    reqs = [_req(verdict="NONE"), _req(verdict="NONE"), _req(confidence="low", verdict="NONE")]
    assert gate.decide(reqs) == gate.DO_NOT_APPLY


def test_a_human_ruling_resolves_an_unsure_item():
    reqs = [_req(confidence="low", verdict="NONE", ruled=True), _req(verdict="HAVE")]
    # Ruled hard=True + NONE now counts as a known hard NONE.
    assert gate.decide(reqs) == gate.CONDITIONAL


def test_empty_requirements_is_an_error_not_a_pass():
    """An empty extraction means the gate learned nothing. Fail closed."""
    assert gate.decide([]) == gate.ERROR


def test_low_confidence_partial_is_unresolved():
    """Pins the PARTIAL half of _is_unresolved's tuple. If PARTIAL were dropped
    from it, this posting would sail through as PROCEED with an unexamined gap."""
    reqs = [_req(confidence="low", verdict="PARTIAL"), _req(verdict="HAVE")]
    assert gate.decide(reqs) == gate.NEEDS_REVIEW


def test_counts_returns_both_tallies():
    reqs = [_req(verdict="NONE"),                        # known hard NONE
            _req(confidence="low", verdict="PARTIAL"),   # unresolved
            _req(verdict="HAVE")]                        # neither
    assert gate.counts(reqs) == {"known_hard_none": 1, "unresolved": 1}
