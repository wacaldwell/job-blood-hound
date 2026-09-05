from datetime import datetime, timedelta, timezone

import fit
import notify


def days_ago(days):
    """ISO timestamp `days` before now, in UTC."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_rank_key_prefers_llm_then_deterministic():
    assert fit.rank_key({"fit_score": 40, "llm_fit_score": 90}) == 90
    assert fit.rank_key({"fit_score": 40, "llm_fit_score": None}) == 40
    assert fit.rank_key({}) == 0


def test_build_digest_orders_and_caps_and_is_clean():
    jobs = [
        {"title": "Solutions Architect", "company": "temporal",
         "fit_score": 70, "llm_fit_score": 88, "llm_coding_bar": "light",
         "url": "http://t", "posted_at": "", "date_source": ""},
        {"title": "Cloud Engineer", "company": "clickhouse",
         "fit_score": 60, "llm_fit_score": None, "llm_coding_bar": None,
         "url": "http://c", "posted_at": "", "date_source": ""},
    ]
    text = fit.build_digest(jobs, {"discovered": 50, "queued": 2}, limit=10)
    assert "\u2014" not in text  # em dash absent (escape, not a literal char)
    # Highest-ranked appears before the lower one.
    assert text.index("Solutions Architect") < text.index("Cloud Engineer")
    assert "88" in text
    assert "discovered" in text


def test_build_digest_respects_limit():
    jobs = [{"title": f"Role {i}", "company": "x", "fit_score": i,
             "llm_fit_score": None, "llm_coding_bar": None, "url": "u",
             "posted_at": "", "date_source": ""} for i in range(20)]
    text = fit.build_digest(jobs, {}, limit=5)
    assert text.count("Role ") == 5


def test_build_digest_is_compact_one_line_per_lead():
    jobs = [
        {"title": "Solutions Architect", "company": "elastic",
         "fit_score": 70, "llm_fit_score": 78, "llm_coding_bar": "light",
         "location_type": "remote", "url": "http://e",
         "posted_at": "2026-06-19T00:00:00Z", "date_source": "ashby:publishedAt"},
    ]
    text = fit.build_digest(jobs, {"discovered": 5}, limit=10)
    # The lead is a single line carrying score, loc, company, title, and link.
    lead_lines = [ln for ln in text.splitlines() if "Solutions Architect" in ln]
    assert len(lead_lines) == 1
    line = lead_lines[0]
    assert "78" in line and "rem" in line and "elastic" in line
    assert "[open](http://e)" in line
    assert "\u2014" not in text


def test_digest_preserves_double_hyphen_in_url():
    # Workday URLs (built from externalPath) contain "--"; the digest must not
    # run them through _no_dash, which would corrupt the link.
    url = "https://waystar.wd1.myworkdayjobs.com/W/job/Atlanta-GA/VP--Finance_R3268"
    jobs = [{"title": "Staff SRE", "company": "waystar", "fit_score": 70,
             "llm_fit_score": 72, "llm_coding_bar": "light",
             "location_type": "remote", "url": url,
             "posted_at": "", "date_source": ""}]
    text = fit.build_digest(jobs, {}, limit=10)
    assert f"[open]({url})" in text  # URL intact, "--" preserved


def test_digest_still_sanitizes_em_dash_in_display_text():
    jobs = [{"title": "Staff SRE \u2014 Platform", "company": "acme",
             "fit_score": 70, "llm_fit_score": 72, "url": "",
             "posted_at": "", "date_source": ""}]
    text = fit.build_digest(jobs, {}, limit=10)
    assert "\u2014" not in text  # display text still cleaned


def test_short_age_marks_approximate_and_unknown():
    assert fit._short_age("", "") == "?"
    # Greenhouse approximate dates get a leading ~.
    approx = fit._short_age("2020-01-01T00:00:00Z", "greenhouse:updated_at~")
    assert approx.startswith("~")


def test_sort_key_prefers_fresh_within_vetted():
    # A fresh modest lead outranks a stale stronger one (an old listing is
    # usually filled). Both are vetted, so freshness is the deciding factor.
    #
    # The ages are relative to now on purpose. _age_penalty measures against
    # the clock, so hardcoded dates are a time bomb: this test was written with
    # a "fresh" 2026-06-20 and started failing once that date aged past the
    # 10-point score gap (~70 days at ~1 point per week), which is roughly
    # 2026-08-28. A test about relative freshness has to state ages relatively.
    fresh = {"llm_fit_score": 72, "posted_at": days_ago(2),
             "date_source": "ashby:publishedAt"}
    stale = {"llm_fit_score": 82, "posted_at": days_ago(180),
             "date_source": "greenhouse:first_published"}
    assert fit.sort_key(fresh) > fit.sort_key(stale)


def test_sort_key_puts_vetted_before_unvetted():
    # A vetted lead with a LOW llm score still sorts above an un-vetted lead
    # with a HIGH deterministic score: a verdict is the trusted signal.
    vetted_low = {"fit_score": 90, "llm_fit_score": 30}
    unvetted_high = {"fit_score": 90, "llm_fit_score": None}
    assert fit.sort_key(vetted_low) > fit.sort_key(unvetted_high)
    # Within the vetted tier, higher llm score wins.
    vetted_high = {"fit_score": 50, "llm_fit_score": 80}
    assert fit.sort_key(vetted_high) > fit.sort_key(vetted_low)


def test_build_digest_vetted_ranks_above_unvetted():
    jobs = [
        {"title": "Unvetted Strong Title", "company": "x",
         "fit_score": 90, "llm_fit_score": None, "llm_coding_bar": None,
         "url": "http://u", "posted_at": "", "date_source": ""},
        {"title": "Vetted Modest Fit", "company": "y",
         "fit_score": 90, "llm_fit_score": 30, "llm_coding_bar": "heavy",
         "url": "http://v", "posted_at": "", "date_source": ""},
    ]
    text = fit.build_digest(jobs, {}, limit=10)
    # The vetted lead appears first even though both share deterministic 90.
    assert text.index("Vetted Modest Fit") < text.index("Unvetted Strong Title")
    # The pending tier is labeled so the two groups are distinguishable.
    assert "pending verdict" in text.lower()


def test_needs_attention_section_leads_the_digest():
    # This section is the whole point of the feature: it arrives on cron
    # whether or not the human goes looking, which is what the digest's other
    # sections never did for a committed lead.
    stale = [{"uid": "u1", "company": "Globex", "title": "SRE Team Lead",
              "url": "https://example.test/1", "fit_score": 93,
              "llm_fit_score": None, "posted_at": "", "date_source": "",
              "location_type": "", "idle_label": "idle 24d"}]
    text, _ = fit.build_digest_sections([], [], {}, stale=stale)
    assert "Needs attention" in text
    assert "idle 24d" in text
    assert "Globex" in text
    # It must come before the new-leads section, because it is the part
    # about the human rather than about the market.
    assert text.index("Needs attention") < text.index("No new leads today.")


def test_no_section_when_nothing_is_stale():
    # A clean pipeline must stay quiet, or the section becomes wallpaper.
    text, _ = fit.build_digest_sections([], [], {}, stale=[])
    assert "Needs attention" not in text
    text2, _ = fit.build_digest_sections([], [], {}, stale=None)
    assert "Needs attention" not in text2


def test_stale_leads_are_not_marked_digested():
    # shown_uids drives mark_digested, which controls the New vs Still open
    # split. A stale lead is a recurring nag, not a one-time announcement,
    # so it must keep appearing until the human acts.
    stale = [{"uid": "stale-uid", "company": "Globex", "title": "SRE",
              "url": "", "fit_score": 93, "llm_fit_score": None,
              "posted_at": "", "date_source": "", "location_type": "",
              "idle_label": "idle 24d"}]
    _, shown = fit.build_digest_sections([], [], {}, stale=stale)
    assert "stale-uid" not in shown


def test_needs_attention_is_ordered_by_score_not_posting_age():
    # This section's whole subject is idle time, so it must not be ordered by
    # a POSTING-age penalty (which sort_key subtracts). Here the stronger
    # lead has the older posting: under sort_key the age penalty flips the
    # two, under rank_key the displayed score wins, and rank_key is also the
    # number the line prints.
    #
    # Both postings are ordinary ages a real board serves, and both sit inside
    # the 140-day point where _age_penalty saturates at its 20-point cap, so
    # the 5-point score gap really is being overturned by the penalty rather
    # than by an out-of-range input. The dates are relative to now rather than
    # hardcoded so neither one ages into the cap later and quietly stops
    # discriminating. Idle days never exceed posting age, so the idle labels
    # are set under each posting age too.
    weak_fresh = {"uid": "u1", "company": "Freshco", "title": "SRE",
                  "url": "", "fit_score": 80, "llm_fit_score": None,
                  "posted_at": days_ago(10),
                  "date_source": "greenhouse:first_published",
                  "location_type": "", "idle_label": "idle 8d"}
    strong_old = {"uid": "u2", "company": "Oldco", "title": "SRE",
                  "url": "", "fit_score": 85, "llm_fit_score": None,
                  "posted_at": days_ago(50),
                  "date_source": "greenhouse:first_published",
                  "location_type": "", "idle_label": "idle 45d"}
    # Guard: the two keys really do disagree here, so this test can fail.
    assert fit.sort_key(weak_fresh) > fit.sort_key(strong_old)
    text, _ = fit.build_digest_sections([], [], {},
                                        stale=[weak_fresh, strong_old])
    assert text.index("Oldco") < text.index("Freshco")


def _job(uid, title, score):
    return {"uid": uid, "title": title, "company": "acme", "fit_score": score,
            "llm_fit_score": None, "llm_coding_bar": None,
            "location_type": "remote", "url": "https://example.test/j",
            "posted_at": "", "date_source": ""}


def _stale(uid, company, score=90, idle="idle 9d"):
    return {"uid": uid, "company": company, "title": "SRE",
            "url": "https://example.test/x", "fit_score": score,
            "llm_fit_score": None, "posted_at": "", "date_source": "",
            "location_type": "", "idle_label": idle}


def test_needs_attention_is_capped_with_a_more_tail():
    # Unbounded, this section grows until it pushes the New section past the
    # Discord cut. It is capped the same way the seen recap is.
    stale = [_stale(f"u{i}", f"Co{i}", score=i) for i in range(14)]
    text, _ = fit.build_digest_sections([], [], {}, stale=stale,
                                        stale_limit=10)
    assert "Needs attention** (14)" in text   # header still tells the truth
    assert "(+4 more)" in text
    # Only the top 10 by rank_key are rendered; Co0 is the weakest.
    assert "Co13" in text
    assert "Co0**" not in text


def test_no_more_tail_when_the_section_fits():
    stale = [_stale(f"u{i}", f"Co{i}", score=i) for i in range(3)]
    text, _ = fit.build_digest_sections([], [], {}, stale=stale,
                                        stale_limit=10)
    assert "more)" not in text


def test_deliver_limit_drops_uids_whose_line_was_truncated():
    # shown_uids drives mark_digested. A uid past the transport's hard cut
    # was never read, and stamping it burns its one-time New announcement.
    new = [_job("u1", "First Role", 99), _job("u2", "Second Role", 98)]
    full, _ = fit.build_digest_sections(new, [], {})
    first_line_end = full.index("Second Role")
    text, shown = fit.build_digest_sections(new, [], {},
                                            deliver_limit=first_line_end)
    assert shown == ["u1"]
    # The rendered text is unchanged; only the stamping is narrowed.
    assert text == full


def _line_end(text, needle):
    """The offset just past the line containing `needle`, which is exactly the
    `end` _delivered_uids computes for that line: the joined text puts one
    separator between lines, so a line's end is the index of its own newline,
    or the end of the text when it is the last line (join adds no trailing
    separator).
    """
    nl = text.find("\n", text.index(needle))
    return len(text) if nl == -1 else nl


def test_deliver_limit_boundary_is_exact_at_the_first_line():
    # The cut is off-by-one sensitive in the direction that loses data: one
    # character of slack stamps a lead the transport never delivered, and a
    # burnt "New" announcement does not come back. Pin both sides.
    new = [_job("u1", "First Role", 99), _job("u2", "Second Role", 98)]
    full, _ = fit.build_digest_sections(new, [], {})
    end1 = _line_end(full, "First Role")

    # A line that exactly fills the budget was delivered whole.
    _, shown = fit.build_digest_sections(new, [], {}, deliver_limit=end1)
    assert shown == ["u1"]
    # One character short and it was cut; a partial line is a lost lead.
    _, shown = fit.build_digest_sections(new, [], {}, deliver_limit=end1 - 1)
    assert shown == []


def test_deliver_limit_counts_the_newline_between_lines():
    # Each joined line costs len(line) + 1. Forgetting the separator makes
    # every later line look one character cheaper than it is, which stamps an
    # undelivered lead once the drift reaches the cut.
    new = [_job("u1", "First Role", 99), _job("u2", "Second Role", 98)]
    full, _ = fit.build_digest_sections(new, [], {})
    end2 = _line_end(full, "Second Role")

    _, shown = fit.build_digest_sections(new, [], {}, deliver_limit=end2)
    assert shown == ["u1", "u2"]
    _, shown = fit.build_digest_sections(new, [], {}, deliver_limit=end2 - 1)
    assert shown == ["u1"]


def test_deliver_limit_none_stamps_everything():
    new = [_job("u1", "First Role", 99)]
    _, shown = fit.build_digest_sections(new, [], {})
    assert shown == ["u1"]


def test_a_huge_stale_section_never_stamps_leads_it_pushed_past_the_cut():
    # The exact compounding harm: an oversized Needs attention section
    # truncates the New section away, and cmd_refine would then stamp leads
    # that were never delivered.
    stale = [_stale(f"s{i}", f"Company-Number-{i}") for i in range(40)]
    new = [_job("n1", "Pushed Off The End", 95)]
    text, shown = fit.build_digest_sections(new, [], {}, stale=stale,
                                            stale_limit=40,
                                            deliver_limit=notify.DISCORD_LIMIT)
    # Guard: this only tests anything if the lead really was cut off.
    assert "Pushed Off The End" not in text[:notify.DISCORD_LIMIT]
    assert shown == []
    # And stale leads are still never stamped, capped or not.
    assert not any(u.startswith("s") for u in shown)


# --- the do_not_claim ledger outranks a cached verdict ---------------------
#
# score() caps a ledger-tripping lead at LEDGER_CAP, but that cap only ever
# reached `fit_score`. A lead with an `llm_fit_score` cached from an earlier
# refine (or from the lead-inbox ingest) kept displaying that verdict and
# kept sorting in the vetted tier above every unvetted lead, so a forbidden
# lead scored 95 still ranked first on a deterministic score of 25. The verdict
# was formed WITHOUT the ledger, so for these leads it is not a trusted signal.
# fit_reasons is where score() records the hit; it is the structured reason
# column, and it is reloaded with the row.

def test_rank_key_caps_a_cached_verdict_the_ledger_refuses():
    forbidden = {"fit_score": 25, "llm_fit_score": 95,
                 "fit_reasons": "title:senior; ledger:data catalog"}
    assert fit.rank_key(forbidden) == fit.LEDGER_CAP


def test_rank_key_leaves_a_clean_verdict_alone():
    assert fit.rank_key({"fit_score": 40, "llm_fit_score": 95,
                         "fit_reasons": "title:senior; remote"}) == 95
    assert fit.rank_key({"fit_score": 40, "llm_fit_score": 95,
                         "fit_reasons": None}) == 95


def test_rank_key_does_not_raise_a_low_score_to_the_cap():
    """The ledger is a ceiling, never a floor, at every layer."""
    assert fit.rank_key({"fit_score": 10, "llm_fit_score": None,
                         "fit_reasons": "ledger:kubernetes admin"}) == 10


def test_sort_key_drops_a_ledger_hit_out_of_the_vetted_tier():
    forbidden = {"fit_score": 25, "llm_fit_score": 95, "posted_at": None,
                 "fit_reasons": "ledger:data catalog"}
    clean = {"fit_score": 60, "llm_fit_score": None, "posted_at": None,
             "fit_reasons": "title:senior"}
    assert fit.sort_key(clean) > fit.sort_key(forbidden)


def test_a_reason_merely_containing_the_word_is_not_a_ledger_hit():
    assert fit.rank_key({"fit_score": 40, "llm_fit_score": 95,
                         "fit_reasons": "note:ledgering"}) == 95
