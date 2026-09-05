"""Tests for openjobs.py, the wide-net second discovery source.

No test touches the network: every outbound boundary (the embed call and the
static-file GET) is injected. The corpus fixtures below are hand-built and
tiny, so the shapes under test are the ones the code actually parses rather
than a 14MB recorded manifest nobody reads.
"""
import base64
import json
import time
import struct

import pytest

import openjobs


# -- fixtures ---------------------------------------------------------------

DIMS = 4


def vec(*xs):
    """A DIMS-length float32 vector, base64'd the way group files carry them."""
    return base64.b64encode(struct.pack(f"<{DIMS}f", *xs)).decode()


def manifest(built_at=1788133780418):
    """Two leaves under one root. Leaf 1 points at +x, leaf 2 at +y."""
    return {
        "recipe": "text-embedding-3-small:1536:v3",
        "dims": DIMS,
        "jobs": 2070108,
        "leaves": 2,
        "built_at": built_at,
        "tree": [
            {"id": 0, "children": [1, 2], "label": "root", "size": 2,
             "exemplars": []},
            {"id": 1, "children": [], "label": "cloud ops", "size": 2,
             "exemplars": []},
            {"id": 2, "children": [], "label": "line cooks", "size": 1,
             "exemplars": []},
        ],
    }


def centroids():
    """float16 matrix, one row per node id. Row 1 is +x, row 2 is +y."""
    import numpy as np
    return np.array([[0.5, 0.5, 0, 0],
                     [1.0, 0.0, 0, 0],
                     [0.0, 1.0, 0, 0]], dtype="float16").tobytes()


GROUP_1 = {
    "jobs": [
        {"ats": "greenhouse", "slug": "federato", "id": "5382500008",
         "title": "Director of Infrastructure", "company": "Federato",
         "location": "Remote", "url": "https://job-boards.greenhouse.io/federato/jobs/5382500008",
         "seen": 1787000000000, "jd": "Lead Infrastructure and SRE. Terraform, AWS.",
         "v": vec(1.0, 0.0, 0.0, 0.0)},
        {"ats": "lever", "slug": "filevine", "id": "e57a5f16",
         "title": "Staff Site Reliability Engineer", "company": "Filevine",
         "location": "Remote", "url": "https://jobs.lever.co/filevine/e57a5f16",
         "seen": 1787500000000, "jd": "SRE, observability, Kubernetes.",
         "v": vec(0.9, 0.1, 0.0, 0.0)},
    ]
}

GROUP_2 = {
    "jobs": [
        {"ats": "greenhouse", "slug": "dinerco", "id": "99",
         "title": "Line Cook", "company": "DinerCo",
         "location": "Portland, OR", "url": "https://example.com/cook",
         "seen": 1787900000000, "jd": "Cook food.", "v": vec(0.0, 1.0, 0.0, 0.0)},
    ]
}


def fake_get(payloads):
    """A `get` stand-in that serves recorded payloads by path."""
    calls = []

    def get(path, binary=False):
        calls.append(path)
        if path not in payloads:
            raise openjobs.CorpusError(f"unexpected path {path}")
        return payloads[path]

    get.calls = calls
    return get


def corpus(**overrides):
    payloads = {
        "/data/manifest.json": manifest(),
        "/data/centroids.bin": centroids(),
        "/data/groups/1.json": GROUP_1,
        "/data/groups/2.json": GROUP_2,
    }
    payloads.update(overrides)
    return fake_get(payloads)


CFG = {
    "title_terms": ["infrastructure", "site reliability", "cloud"],
    "exclude_terms": ["intern"],
    "location_terms": ["remote", "united states", "portland"],
}


# -- the ideal-JD vector cache ---------------------------------------------

def test_vector_is_embedded_once_and_cached_by_text_hash(tmp_path):
    """The embed call is the only thing that leaves the machine. It must fire
    on a new JD and never again while that JD is unchanged."""
    calls = []

    def embed(text):
        calls.append(text)
        return [1.0, 0.0, 0.0, 0.0]

    first = openjobs.ideal_vector("cloud ops role", cache=tmp_path, embed=embed)
    second = openjobs.ideal_vector("cloud ops role", cache=tmp_path, embed=embed)

    assert first == second
    assert len(calls) == 1, "a second run with an unchanged JD re-embedded"


def test_editing_the_jd_re_embeds(tmp_path):
    calls = []

    def embed(text):
        calls.append(text)
        return [1.0, 0.0, 0.0, 0.0]

    openjobs.ideal_vector("cloud ops role", cache=tmp_path, embed=embed)
    openjobs.ideal_vector("platform lead role", cache=tmp_path, embed=embed)

    assert len(calls) == 2
    assert calls[1] == "platform lead role"


def test_cached_vector_records_the_text_that_produced_it(tmp_path):
    """The 2026-08-31 hand-run left two ideal-JD versions on disk with no record
    of which produced the shortlist. The cache must not be able to repeat that."""
    openjobs.ideal_vector("cloud ops role", cache=tmp_path,
                          embed=lambda t: [1.0, 0.0, 0.0, 0.0])
    saved = json.loads((tmp_path / "ideal.json").read_text())
    assert saved["sha256"] == openjobs.text_hash("cloud ops role")
    assert saved["source_text"] == "cloud ops role"


# -- group ranking ----------------------------------------------------------

def test_rank_groups_orders_leaves_by_cosine_and_skips_internal_nodes():
    m = manifest()
    ranked = openjobs.rank_groups(m, openjobs.load_centroids(centroids(), DIMS),
                                  [1.0, 0.0, 0.0, 0.0], k=5)
    assert [leaf for leaf, _ in ranked] == [1, 2], "root node 0 must not rank"
    assert ranked[0][1] > ranked[1][1]


def test_rank_groups_respects_k():
    m = manifest()
    ranked = openjobs.rank_groups(m, openjobs.load_centroids(centroids(), DIMS),
                                  [1.0, 0.0, 0.0, 0.0], k=1)
    assert len(ranked) == 1


# -- candidate mapping ------------------------------------------------------

def test_discover_maps_a_posting_into_the_scanner_job_shape(tmp_path):
    got = openjobs.discover(CFG, cache=tmp_path, get=corpus(),
                            embed=lambda t: [1.0, 0.0, 0.0, 0.0],
                            jd_text="cloud ops", groups=2)
    fed = next(j for j in got if j["company"] == "federato")
    assert fed["ats"] == "greenhouse"
    assert fed["id"] == "5382500008"
    assert fed["company"] == "federato", "company must be the board slug, for uid dedup"
    assert fed["title"] == "Director of Infrastructure"
    assert fed["url"].endswith("/5382500008")
    assert fed["source"] == "openjobs"
    assert fed["location_type"] == "remote"
    assert "Terraform" in fed["description"], "full JD must ride along from the corpus"


def test_posting_date_provenance_is_labelled_approximate(tmp_path):
    """`seen` is when the crawler first saw the posting, not when it was posted.
    CLAUDE.md requires that be labelled honestly with a trailing ~."""
    got = openjobs.discover(CFG, cache=tmp_path, get=corpus(),
                            embed=lambda t: [1.0, 0.0, 0.0, 0.0],
                            jd_text="cloud ops", groups=2)
    fed = next(j for j in got if j["company"] == "federato")
    assert fed["date_source"].endswith("~")
    assert fed["posted_at"].startswith("2026-")


def test_discover_sorts_by_similarity_descending(tmp_path):
    got = openjobs.discover(CFG, cache=tmp_path, get=corpus(),
                            embed=lambda t: [1.0, 0.0, 0.0, 0.0],
                            jd_text="cloud ops", groups=2)
    sims = [j["sim"] for j in got]
    assert sims == sorted(sims, reverse=True)


def test_discover_applies_title_and_exclude_terms(tmp_path):
    """Embedding similarity ranks well and filters badly. The Line Cook is
    semantically distant but must be excluded by the term list, not by luck."""
    got = openjobs.discover(CFG, cache=tmp_path, get=corpus(),
                            embed=lambda t: [1.0, 0.0, 0.0, 0.0],
                            jd_text="cloud ops", groups=2)
    assert "dinerco" not in {j["company"] for j in got}


# -- failure behaviour: discovery fails SAFE --------------------------------

def test_network_failure_yields_no_candidates_rather_than_raising(tmp_path):
    def boom(path, binary=False):
        raise openjobs.CorpusError("502 from the worker")

    assert openjobs.discover(CFG, cache=tmp_path, get=boom,
                             embed=lambda t: [1.0, 0.0, 0.0, 0.0],
                             jd_text="cloud ops") == []


def test_a_failed_embed_yields_no_candidates(tmp_path):
    def boom(text):
        raise openjobs.CorpusError("429 rate limited")

    assert openjobs.discover(CFG, cache=tmp_path, get=corpus(), embed=boom,
                             jd_text="cloud ops") == []


def test_missing_ideal_jd_yields_no_candidates(tmp_path):
    assert openjobs.discover(CFG, cache=tmp_path, get=corpus(),
                             embed=lambda t: [1.0, 0, 0, 0], jd_text="") == []


# -- per-posting similarity -------------------------------------------------

def test_similarity_is_per_posting_not_per_group():
    """Every posting in a group must NOT share the group's centroid score.

    Caught on the first live run: ranking by centroid gave all 15 ingested
    leads an identical 0.796, so within the best group the order was insertion
    order and the top-N cap was taking the first 15 rows of a JSON file rather
    than the 15 best matches. The group centroid picks WHICH groups to
    download; the posting's own vector is what ranks inside them.
    """
    ranked = openjobs.score_postings(GROUP_1["jobs"], [1.0, 0.0, 0.0, 0.0])
    sims = [s for _, s in ranked]
    assert len(set(sims)) == 2, f"postings share a score: {sims}"
    assert sims[0] > sims[1]
    assert ranked[0][0]["slug"] == "federato", "the +x posting must win"


def test_discover_ranks_postings_within_a_group(tmp_path):
    got = openjobs.discover(CFG, cache=tmp_path, get=corpus(),
                            embed=lambda t: [1.0, 0.0, 0.0, 0.0],
                            jd_text="cloud ops", groups=2)
    sims = [j["sim"] for j in got]
    assert len(set(sims)) == len(sims), f"postings share a score: {sims}"
    assert got[0]["company"] == "federato"


def test_a_posting_with_no_vector_is_kept_at_the_group_score(tmp_path):
    """A missing or corrupt vector must cost that one posting its precision,
    not its place in the run."""
    ranked = openjobs.score_postings(
        [{"slug": "x", "v": None}], [1.0, 0.0, 0.0, 0.0], fallback=0.42)
    assert ranked[0][1] == 0.42


# -- company naming ---------------------------------------------------------

def test_the_board_slug_is_never_rewritten():
    """`company` is the uid's middle field AND, for four ATSes, a path segment
    in job_generate.posting_endpoint. It is stored exactly as published.

    An earlier version prettified it, which broke two ways at once.
    A domain-shaped board slug like `globex.com` is a real Ashby shape (that
    slug's API returns 200 while the bare `globex` returns 404), so a prettified
    slug made the gate's fetch 404 and return ERROR,
    which blocks drafting exactly like DO_NOT_APPLY. And `.split(".")[0]`
    collapsed `careers.acme.com` and `careers.beta.com` to the same `careers`,
    so two employers sharing a per-tenant requisition number produced one uid
    and the second lead was silently counted a duplicate.

    Display is a separate column. Nothing about a name a human reads is allowed
    to touch a key or a URL.
    """
    for slug in ("globex.com", "careers.acme.com", "www.foo.com",
                 "umbralabs.wd1.myworkdayjobs.com", "vertexanalytics",
                 "6AA7121EF2B44AA4A85D1E2E3DC30F4E"):
        assert openjobs.to_job(
            {"ats": "greenhouse", "slug": slug, "id": "1", "title": "Cloud Engineer",
             "location": "Remote", "url": "u", "jd": "", "company": "Pretty Name"},
            0.5, [])["company"] == slug


def test_two_hostname_boards_stay_distinct():
    """The false-dedup case, stated as a key collision rather than a string."""
    made = [openjobs.to_job(
        {"ats": "oraclecloud", "slug": s, "id": "R123", "title": "Cloud Engineer",
         "location": "Remote", "url": f"https://{s}/R123", "jd": "", "company": ""},
        0.5, []) for s in ("careers.acme.com", "careers.beta.com")]
    assert made[0]["company"] != made[1]["company"]


def test_display_name_prefers_the_published_company_name():
    assert openjobs.display_name(
        "magnitudesoftware.wd1.myworkdayjobs.com", "insightsoftware") == "insightsoftware"
    assert openjobs.display_name("junipersquare", "Juniper Square") == "Juniper Square"
    assert openjobs.display_name("saasgroup", "saas.group") == "saas.group"


def test_display_name_falls_back_to_the_hosts_first_label():
    assert openjobs.display_name(
        "ghr.wd1.myworkdayjobs.com", "ghr.wd1.myworkdayjobs.com") == "ghr"


def test_display_name_keeps_an_opaque_id_rather_than_inventing_one():
    h = "6AA7121EF2B44AA4A85D1E2E3DC30F4E"
    assert openjobs.display_name(h, h) == h


# -- per-posting similarity -------------------------------------------------

# -- cache growth -----------------------------------------------------------

def test_group_files_are_cached_under_the_build_that_produced_them(tmp_path):
    """Group ids are NOT stable between the corpus's daily rebuilds, so a cache
    keyed on id alone serves yesterday's postings under today's numbering."""
    openjobs.group_jobs(1, 1788133780418, get=corpus(), cache=tmp_path)
    assert (tmp_path / "groups" / "1788133780418" / "1.json").exists()


def test_a_rebuild_does_not_reuse_the_previous_builds_group_file(tmp_path):
    get = corpus()
    openjobs.group_jobs(1, 111, get=get, cache=tmp_path)
    openjobs.group_jobs(1, 222, get=get, cache=tmp_path)
    assert get.calls.count("/data/groups/1.json") == 2, "served a stale build's group"


def test_old_builds_are_pruned_so_the_cache_does_not_grow_without_bound(tmp_path):
    """~37MB of group files per build, every day, on a host with 8GB free.
    Unpruned that is 13GB a year and a full disk."""
    get = corpus()
    builds = [1788000000000, 1788100000000, 1788200000000, 1788300000000]
    for build in builds:
        openjobs.group_jobs(1, build, get=get, cache=tmp_path)
    openjobs.prune_cache(cache=tmp_path, keep=2)
    left = sorted(d.name for d in (tmp_path / "groups").iterdir())
    assert left == ["1788200000000", "1788300000000"]


def test_pruning_keeps_the_NEWEST_builds_even_with_uneven_name_lengths(tmp_path):
    """A string sort puts "9" above "1788300000000" and would delete the
    newest build while keeping a stale one."""
    get = corpus()
    for build in (9, 1788200000000, 1788300000000):
        openjobs.group_jobs(1, build, get=get, cache=tmp_path)
    openjobs.prune_cache(cache=tmp_path, keep=2)
    left = sorted(d.name for d in (tmp_path / "groups").iterdir())
    assert left == ["1788200000000", "1788300000000"]


def test_discover_prunes_the_cache_on_every_run(tmp_path):
    """prune_cache existed but nothing called it, which is the same as not
    having written it."""
    # Two older builds plus the one discover() will write is three, and keep=2
    # must drop the oldest.
    oldest = tmp_path / "groups" / "1788000000000"
    middle = tmp_path / "groups" / "1788100000000"
    for d in (oldest, middle):
        d.mkdir(parents=True)
        (d / "old.json").write_text("{}")
    openjobs.discover(CFG, cache=tmp_path, get=corpus(),
                      embed=lambda t: [1.0, 0.0, 0.0, 0.0],
                      jd_text="cloud ops", groups=2)
    assert not oldest.exists(), "discover() left the oldest build behind"
    assert middle.exists(), "discover() pruned a build it should have kept"


def test_a_pruning_failure_never_costs_the_run_its_candidates(tmp_path):
    """Housekeeping must not be able to lose leads."""
    def boom(cache=None, keep=2):
        raise OSError("read-only filesystem")

    saved = openjobs.prune_cache
    openjobs.prune_cache = boom
    try:
        got = openjobs.discover(CFG, cache=tmp_path, get=corpus(),
                                embed=lambda t: [1.0, 0.0, 0.0, 0.0],
                                jd_text="cloud ops", groups=2)
    finally:
        openjobs.prune_cache = saved
    assert len(got) == 2


# -- the vector cache must follow the corpus, not just the JD ---------------

def test_a_new_embedding_model_at_the_same_dims_forces_a_re_embed():
    """The silent-garbage case. Keying the cache on the JD text alone means
    that when upstream swaps the embedding model but keeps 1536 dims, a stale
    vector is ranked against a new space. The run is not empty, it is fifteen
    confidently ranked leads of semantic noise, and nothing logs."""
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        calls = []

        def embed(text):
            calls.append(text)
            return [1.0, 0.0, 0.0, 0.0]

        openjobs.ideal_vector("jd", cache=d, embed=embed,
                              recipe="model-A:1536:v3", dims=DIMS)
        openjobs.ideal_vector("jd", cache=d, embed=embed,
                              recipe="model-B:1536:v4", dims=DIMS)
        assert len(calls) == 2, "reused a vector from a different embedding space"


def test_a_dims_change_re_embeds_rather_than_locking_the_stage_off():
    """The permanent-death case. Bailing out on a mismatch without ever
    replacing the cached vector meant every later run returned nothing,
    forever, and looked exactly like a quiet day."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        calls = []

        def embed(text):
            calls.append(text)
            return [1.0, 0.0, 0.0, 0.0]

        openjobs.ideal_vector("jd", cache=d, embed=embed, recipe="r", dims=DIMS)
        openjobs.ideal_vector("jd", cache=d, embed=embed, recipe="r", dims=99)
        assert len(calls) == 2


def test_an_unchanged_corpus_and_jd_still_costs_no_embed_call():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        calls = []

        def embed(text):
            calls.append(text)
            return [1.0, 0.0, 0.0, 0.0]

        for _ in range(3):
            openjobs.ideal_vector("jd", cache=d, embed=embed, recipe="r", dims=DIMS)
        assert len(calls) == 1


def test_a_rebuilt_corpus_recovers_on_the_very_next_run(tmp_path):
    """End to end across a rebuild, and load-bearing rather than decorative.

    The stale cached vector points at +y, which ranks the wrong group first.
    Only a genuine re-embed restores the +x ordering, so this fails if the
    recipe check is removed.
    """
    get = corpus()
    calls = []

    def embed(text):
        calls.append(text)
        return [1.0, 0.0, 0.0, 0.0]

    first = openjobs.discover(CFG, cache=tmp_path, get=get, embed=embed,
                              jd_text="cloud ops", groups=2)
    assert first and first[0]["company"] == "federato"

    (tmp_path / "ideal.json").write_text(json.dumps({
        "vector": [0.0, 1.0, 0.0, 0.0],  # points elsewhere entirely
        "sha256": openjobs.text_hash("cloud ops"), "source_text": "cloud ops",
        "recipe": "an-older-model", "dims": DIMS}))

    second = openjobs.discover(CFG, cache=tmp_path, get=get, embed=embed,
                               jd_text="cloud ops", groups=2)
    assert len(calls) == 2, "did not re-embed after the recipe changed"
    assert [j["company"] for j in second] == [j["company"] for j in first]


# -- the cap is a budget, and zero means zero -------------------------------

def test_the_ideal_jd_file_actually_being_missing_yields_no_candidates(tmp_path, monkeypatch):
    """The earlier version of this test passed jd_text="", which exercises the
    empty-string branch and never the OSError from reading the file."""
    monkeypatch.setattr(openjobs, "IDEAL_JD_PATH",
                        tmp_path / "definitely-not-here.md")
    assert openjobs.discover(CFG, cache=tmp_path, get=corpus(),
                             embed=lambda t: [1.0, 0, 0, 0]) == []


def test_one_bad_group_does_not_cost_the_other_groups_their_postings(tmp_path):
    """The earlier version asserted `got == [] or all(...)`, and the real result
    WAS [], so it passed through the escape branch and proved nothing. It would
    have passed even if a corrupt group aborted the whole run."""
    good = {"jobs": [dict(GROUP_1["jobs"][0], slug="survivor",
                          v=vec(0.0, 1.0, 0.0, 0.0))]}
    get = corpus(**{"/data/groups/1.json": {"jobs": [{"broken": True}]},
                    "/data/groups/2.json": good})
    got = openjobs.discover(CFG, cache=tmp_path, get=get,
                            embed=lambda t: [1.0, 0.0, 0.0, 0.0],
                            jd_text="cloud ops", groups=2)
    assert [j["company"] for j in got] == ["survivor"]


# -- malformed corpus payloads must not escape as exceptions ----------------

MALFORMED = [
    ("manifest is a string", {"/data/manifest.json": "nope"}),
    ("manifest tree is a string", {"/data/manifest.json": dict(manifest(), tree="nope")}),
    ("manifest tree holds strings", {"/data/manifest.json": dict(manifest(), tree=["a", "b"])}),
    ("manifest dims missing", {"/data/manifest.json": {"recipe": "r", "tree": []}}),
    ("manifest dims is a string", {"/data/manifest.json": dict(manifest(), dims="4")}),
    ("manifest dims is a float", {"/data/manifest.json": dict(manifest(), dims=4.0)}),
    ("manifest is null", {"/data/manifest.json": None}),
    ("manifest is a list", {"/data/manifest.json": []}),
    ("group is a list", {"/data/groups/1.json": []}),
    ("group jobs is null", {"/data/groups/1.json": {"jobs": None}}),
    ("posting is null", {"/data/groups/1.json": {"jobs": [None]}}),
    ("group is a string", {"/data/groups/1.json": "nope"}),
    ("group jobs is a string", {"/data/groups/1.json": {"jobs": "nope"}}),
    ("group jobs holds strings", {"/data/groups/1.json": {"jobs": ["a", "b"]}}),
    ("posting missing slug", {"/data/groups/1.json": {"jobs": [{"ats": "greenhouse", "id": "1"}]}}),
    ("centroids are truncated", {"/data/centroids.bin": b"\x00\x01"}),
    ("centroids are not bytes", {"/data/centroids.bin": b""}),
]


@pytest.mark.parametrize("label,override", MALFORMED, ids=[m[0] for m in MALFORMED])
def test_a_malformed_corpus_payload_yields_no_candidates_rather_than_raising(
        label, override, tmp_path, capsys):
    """The stage's contract, and CLAUDE.md, both say nothing raises into
    bin/daily.sh. That was not true: a public worker serving a JSON string
    where an object belongs produced a TypeError or an AttributeError, neither
    of which the narrowed handler catches, and neither of which is a bug in
    this module.

    Exception type cannot tell "their data is wrong" from "my code is wrong",
    so the shapes are validated explicitly and a bad one is a CorpusError.
    """
    got = openjobs.discover(CFG, cache=tmp_path, get=corpus(**override),
                            embed=lambda t: [1.0, 0.0, 0.0, 0.0],
                            jd_text="cloud ops", groups=2, verbose=True)
    assert isinstance(got, list)
    # A silent empty list is indistinguishable from a quiet day, and asserting
    # only on the return value cannot tell which layer fired (or whether the
    # validator fired at all). The reason has to reach the log.
    out = capsys.readouterr().out
    assert "!" in out, f"no reason logged for {label}"


def test_a_real_bug_in_this_module_still_propagates(tmp_path, monkeypatch):
    """The other half of the contract. Swallowing a genuine TypeError as "the
    corpus is down" is how a dead stage passes for a quiet day."""
    def broken(*a, **k):
        raise TypeError("this is a bug in our own code")

    monkeypatch.setattr(openjobs, "to_job", broken)
    with pytest.raises(TypeError):
        openjobs.discover(CFG, cache=tmp_path, get=corpus(),
                          embed=lambda t: [1.0, 0.0, 0.0, 0.0],
                          jd_text="cloud ops", groups=2)


def test_the_manifest_is_published_last_so_a_crash_cannot_pair_mismatched_files(tmp_path):
    """The freshness check reads the MANIFEST's mtime, so it must be the last
    thing to change. Published first, a crash between the two renames left a
    fresh-mtime new manifest beside old centroids, and the next run paired them
    and indexed off the end of the matrix: silently wrong, no exception.
    """
    import os as _os
    get = corpus()
    openjobs.load_manifest(get=get, cache=tmp_path)
    # Age the cache so the next call refetches.
    old = time.time() - 7 * 3600
    _os.utime(tmp_path / "manifest.json", (old, old))

    real_replace = openjobs.os.replace
    calls = []

    def fail_on_second(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("killed between publishes")
        real_replace(src, dst)

    openjobs.os.replace = fail_on_second
    try:
        with pytest.raises(OSError):
            openjobs.load_manifest(get=get, cache=tmp_path)
    finally:
        openjobs.os.replace = real_replace

    # The manifest must still be the OLD one, so its mtime still reads stale
    # and the next run refetches rather than trusting a mismatched pair.
    assert _os.path.getmtime(tmp_path / "manifest.json") == pytest.approx(old, abs=2)


def test_a_posting_whose_vector_is_junk_keeps_its_place_at_the_group_score():
    """A corrupt vector costs that one posting its precision, never its place
    in the run."""
    for bad in (None, "not base64", "", 12345, b"\x00"):
        ranked = openjobs.score_postings([{"slug": "x", "v": bad}],
                                         [1.0, 0.0, 0.0, 0.0], fallback=0.42)
        assert ranked[0][1] == 0.42, bad
