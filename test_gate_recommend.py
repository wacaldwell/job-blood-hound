"""RECOMMEND: the gate says yes, not just 'not no'.

The gate was a pure disqualifier. For a job the candidate is clearly strong for
it returned NEEDS_REVIEW ("go rule these four items"), which reads as a hedge, not
a recommendation. RECOMMEND is the positive tier: a strong HAVE ratio with no
known gaps, guarded by a worst-case check so a green light can never hide a
disqualifier.
"""
import gate


def _req(hard=True, confidence="high", verdict="HAVE", ruled=False):
    return {"quote": "q", "topic": "t", "hard": hard, "confidence": confidence,
            "verdict": verdict, "evidence": "e", "bridge": "", "forced": "",
            "ruled_by_human": ruled}


def _strong_haves(n):
    return [_req(verdict="HAVE") for _ in range(n)]


def test_a_strong_match_with_no_gaps_recommends():
    reqs = _strong_haves(8)
    assert gate.decide(reqs) == gate.RECOMMEND


def test_a_strong_match_with_a_few_unsure_items_still_recommends():
    """The Valon case. Many HAVEs, zero known gaps, a handful of low-confidence
    PARTIAL items. Even if all are ruled HARD they stay PARTIAL, not gaps, so the
    worst case is clean and it is safe to recommend."""
    reqs = _strong_haves(10) + [_req(confidence="low", verdict="PARTIAL") for _ in range(4)]
    assert gate.decide(reqs) == gate.RECOMMEND


def test_recommend_never_hides_two_possible_gaps():
    """The safety guard. Two unsure items that are NONE could each become a hard
    gap if ruled HARD. That is a possible DO_NOT_APPLY, so it must go to review,
    never recommend, no matter how many HAVEs surround them."""
    reqs = _strong_haves(12) + [_req(confidence="low", verdict="NONE"),
                                _req(confidence="low", verdict="NONE")]
    assert gate.decide(reqs) == gate.NEEDS_REVIEW


def test_one_possible_gap_routes_to_review_not_recommend():
    """A single unsure NONE is a POSSIBLE gap. Even amid strong coverage it must
    not get a 'strong match, apply' green light; route it to the human to rule, since
    he is the final gauge. He rules it, then it recomputes to RECOMMEND or
    CONDITIONAL."""
    reqs = _strong_haves(10) + [_req(confidence="low", verdict="NONE")]
    assert gate.decide(reqs) == gate.NEEDS_REVIEW


def test_a_soft_have_ratio_cannot_inflate_a_partial_hard_req_to_recommend():
    """fit_strength must count HARD HAVEs, not all HAVEs. Three soft nice-to-haves
    graded HAVE plus one hard PARTIAL is not a STRONG match and must not RECOMMEND."""
    reqs = [_req(hard=False, verdict="HAVE") for _ in range(3)] + [_req(verdict="PARTIAL")]
    assert gate.fit_strength(reqs) != "STRONG"
    assert gate.decide(reqs) != gate.RECOMMEND


def test_a_known_gap_is_never_a_recommend():
    """A confident hard NONE is a real gap. Strength does not paper over it."""
    reqs = _strong_haves(10) + [_req(verdict="NONE")]
    assert gate.decide(reqs) == gate.CONDITIONAL


def test_two_known_gaps_still_do_not_apply_despite_strength():
    reqs = _strong_haves(20) + [_req(verdict="NONE"), _req(verdict="NONE")]
    assert gate.decide(reqs) == gate.DO_NOT_APPLY


def test_a_thin_match_does_not_recommend():
    """Two HAVEs and a soft miss is not a strong match. It proceeds, but it does
    not earn a recommendation."""
    reqs = [_req(verdict="HAVE"), _req(verdict="HAVE"), _req(hard=False, verdict="NONE")]
    assert gate.decide(reqs) == gate.PROCEED


def test_recommend_allows_drafting_like_proceed():
    """require_pass must treat RECOMMEND as a green light."""
    import jobdb
    import tempfile, os
    d = tempfile.mkdtemp()
    db = jobdb.JobDB(os.path.join(d, "t.db"))
    db.upsert_job({"ats": "greenhouse", "company": "valon", "id": "1",
                   "title": "Engineering Manager, Platform", "location": "Remote"})
    uid = jobdb.make_job_uid("greenhouse", "valon", "1")
    db.set_gate(uid, gate.RECOMMEND, "{}", "/tmp/r.md")
    gate.require_pass(db, db.get(uid))  # must not raise
