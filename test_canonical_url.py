"""jobdb.canonical_url: what makes two spellings the same posting.

The rule this file exists to enforce: canonicalisation may only ever merge
things that ARE the same posting. A missed merge costs one duplicate row a
human can see and ignore. A wrong merge silently discards a real job, and no
later stage would ever surface it again. The two costs are not comparable, so
every ambiguous case resolves toward keeping the rows apart.
"""
import jobdb


def canon(u):
    return jobdb.canonical_url(u)


# -- merges that must happen ------------------------------------------------

def test_scheme_host_case_www_and_trailing_slash_are_noise():
    same = ["https://www.Example.com/jobs/7/",
            "http://example.com/jobs/7",
            "https://EXAMPLE.com/jobs/7/"]
    assert len({canon(u) for u in same}) == 1


def test_the_greenhouse_board_host_split_is_folded():
    """Greenhouse migrated boards to job-boards.greenhouse.io and both hosts
    are still live, so the scanner and the corpus disagree about the same
    posting. This is the case the URL layer exists for."""
    assert (canon("https://boards.greenhouse.io/federato/jobs/53825")
            == canon("https://job-boards.greenhouse.io/federato/jobs/53825"))


def test_the_evidenced_trackers_and_the_fragment_are_stripped():
    base = "https://job-boards.greenhouse.io/federato/jobs/53825"
    for noise in ("?gh_src=abc", "?utm_source=x&utm_medium=y",
                  "#top", "?gh_src=abc#apply"):
        assert canon(base + noise) == canon(base), noise


# -- merges that must NOT happen -------------------------------------------

def test_a_query_string_that_carries_the_job_id_is_kept():
    """Taleo, BrassRing and some SuccessFactors boards put the requisition id
    in the query, not the path. Dropping the whole query collapsed every
    posting on such a board onto one string, so the first one ingested and
    every later one was silently discarded as a duplicate, permanently, because
    known_urls is derived from the whole table.

    openjobs.py exists partly to reach exactly these ATS families, so this
    would have thrown away the leads the feature was built to find.
    """
    a = "https://acme.taleo.net/careersection/ex/jobdetail.ftl?job=1234"
    b = "https://acme.taleo.net/careersection/ex/jobdetail.ftl?job=9999"
    assert canon(a) != canon(b)


def test_an_unrecognised_parameter_is_never_assumed_to_be_noise():
    """The allowlist is the safe direction: an unknown parameter might be the
    identifier, so it is kept."""
    base = "https://example.com/jobdetail"
    assert canon(base + "?req=1") != canon(base + "?req=2")
    assert canon(base + "?jobId=1") != canon(base + "?jobId=2")


def test_a_known_tracker_next_to_a_real_id_drops_only_the_tracker():
    a = "https://acme.taleo.net/x.ftl?job=1234&utm_source=linkedin"
    b = "https://acme.taleo.net/x.ftl?job=1234"
    assert canon(a) == canon(b)


def test_parameter_order_does_not_change_identity():
    a = "https://example.com/x?job=1&loc=us"
    b = "https://example.com/x?loc=us&job=1"
    assert canon(a) == canon(b)


def test_path_case_is_preserved_because_ats_ids_are_case_sensitive():
    assert canon("https://example.com/jobs/AbC") != canon("https://example.com/jobs/abc")


def test_two_different_paths_stay_different():
    assert canon("https://example.com/jobs/7") != canon("https://example.com/jobs/8")


# -- degenerate input -------------------------------------------------------

def test_empty_and_relative_input_do_not_collapse_together():
    assert canon("") == ""
    assert canon(None) == ""
    assert canon("/jobs/7") != canon("/jobs/8")


def test_an_embedded_greenhouse_board_keeps_its_job_id():
    """`?gh_jid=` is how a Greenhouse board embedded in a company's own careers
    page addresses a posting; nothing else in the URL identifies it. Its
    near-namesake `?gh_src=` is a tracker. Stripping the wrong one merges an
    entire careers page into a single row.

    Real URL shape, from a live posting: five9.com/about/careers/job-detail.
    """
    base = "https://www.five9.com/about/careers/job-detail"
    assert canon(f"{base}?gh_jid=5676430004") != canon(f"{base}?gh_jid=9999999999")
    assert canon(f"{base}?gh_jid=5676430004&gh_src=abc") == canon(f"{base}?gh_jid=5676430004")


def test_a_workday_style_path_id_is_untouched():
    a = "https://magnitudesoftware.wd1.myworkdayjobs.com/External/job/USA---Remote/X_REQ001019"
    b = "https://magnitudesoftware.wd1.myworkdayjobs.com/External/job/USA---Remote/X_REQ000985"
    assert canon(a) != canon(b)


def test_only_evidenced_parameters_are_ever_stripped():
    """The allowlist must not be a guess list.

    This function's whole rule is that an unrecognised parameter might be the
    identifier, so it stays. An allowlist populated from memory rather than
    from observation breaks that rule quietly: every speculative entry is a
    standing chance of merging two real postings. A sweep of 2,165 live corpus
    URLs found exactly one query parameter in circulation, `gh_jid`, and that
    one must be KEPT.

    So the set holds only what has been observed or cited. Add to it when a
    real URL demands it, not when a name sounds like a tracker.
    """
    assert jobdb.TRACKING_PARAMS == {"gh_src"}


def test_a_plausible_sounding_unknown_parameter_still_separates_postings():
    base = "https://careers.example.com/openings"
    for param in ("hub", "trk", "ref", "source", "src", "trackid"):
        assert canon(f"{base}?{param}=42") != canon(f"{base}?{param}=77"), param


def test_utm_parameters_are_stripped_by_prefix():
    base = "https://example.com/jobs/7"
    assert canon(f"{base}?utm_source=li&utm_campaign=x") == canon(base)


def test_repeated_keys_and_blank_values_round_trip_without_merging():
    assert canon("https://example.com/x?a=1&a=2") != canon("https://example.com/x?a=1")
    assert canon("https://example.com/x?a=") != canon("https://example.com/x")


def test_encoded_characters_do_not_collapse_distinct_ids():
    assert canon("https://example.com/x?job=a%20b") != canon("https://example.com/x?job=ab")
