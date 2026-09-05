import pytest
import job_fetch
from job_fetch import FetchError


def test_linkedin_job_id_from_view_url():
    assert job_fetch.linkedin_job_id(
        "https://www.linkedin.com/jobs/view/4012345678/") == "4012345678"


def test_linkedin_job_id_from_slug_view_url():
    assert job_fetch.linkedin_job_id(
        "https://www.linkedin.com/jobs/view/staff-sre-at-acme-4012345678"
    ) == "4012345678"


def test_linkedin_job_id_from_current_job_id_param():
    assert job_fetch.linkedin_job_id(
        "https://www.linkedin.com/jobs/collections/recommended/?currentJobId=3999888777"
    ) == "3999888777"


def test_linkedin_job_id_rejects_non_job_url():
    with pytest.raises(FetchError):
        job_fetch.linkedin_job_id("https://www.linkedin.com/feed/")


def test_is_linkedin_url():
    assert job_fetch.is_linkedin_url("https://www.linkedin.com/jobs/view/1/")
    assert job_fetch.is_linkedin_url("https://linkedin.com/jobs/view/1/")
    assert not job_fetch.is_linkedin_url("https://boards.greenhouse.io/acme/jobs/1")


JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org/","@type":"JobPosting",
 "title":"Staff Platform Engineer",
 "datePosted":"2026-07-01",
 "hiringOrganization":{"@type":"Organization","name":"Acme Corp"},
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
   "addressLocality":"Beaverton","addressRegion":"OR","addressCountry":"US"}},
 "description":"<p>Build the <b>platform</b>.</p><p>Own reliability.</p>"}
</script>
</head><body><h1>ignored</h1></body></html>
"""

GRAPH_PAGE = """
<html><head><script type="application/ld+json">
{"@graph":[{"@type":"WebSite","name":"x"},
 {"@type":["JobPosting"],"title":"SRE",
  "hiringOrganization":[{"name":"Beta"}],
  "jobLocation":[{"address":"Remote, US"}],
  "description":"Run the site."}]}
</script></head><body></body></html>
"""

NO_POSTING_PAGE = "<html><head><title>Careers</title></head><body>jobs</body></html>"

MALFORMED_JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">{not json at all</script>
<script type="application/ld+json">
{"@type":"JobPosting","title":"After Malformed","description":"D"}
</script>
</head><body></body></html>
"""


def test_parse_jsonld_posting_full():
    p = job_fetch.parse_jsonld_posting(JSONLD_PAGE)
    assert p["title"] == "Staff Platform Engineer"
    assert p["company"] == "Acme Corp"
    assert p["location"] == "Beaverton, OR, US"
    assert "Build the platform." in p["description"]
    assert "Own reliability." in p["description"]
    assert "<p>" not in p["description"]
    assert p["posted_at"] == "2026-07-01"


def test_parse_jsonld_posting_graph_and_lists():
    p = job_fetch.parse_jsonld_posting(GRAPH_PAGE)
    assert p["title"] == "SRE"
    assert p["company"] == "Beta"
    assert p["location"] == "Remote, US"
    assert p["posted_at"] == ""


def test_parse_jsonld_posting_none_when_absent():
    assert job_fetch.parse_jsonld_posting(NO_POSTING_PAGE) is None


def test_parse_jsonld_posting_skips_malformed_blocks():
    p = job_fetch.parse_jsonld_posting(MALFORMED_JSONLD_PAGE)
    assert p["title"] == "After Malformed"


from bs4 import BeautifulSoup

GUEST_FRAGMENT = """
<section>
  <h3 class="topcard__title">Senior SRE</h3>
  <a class="topcard__org-name-link" href="/company/acme">Acme Corp</a>
  <span class="topcard__flavor--bullet">Raleigh, NC</span>
  <code id="applyUrl"><!--"https:\\/\\/boards.greenhouse.io\\/acme\\/jobs\\/777"--></code>
  <div class="show-more-less-html__markup">Run <b>prod</b>. Keep it up.</div>
</section>
"""

APPLY_ANCHOR_PAGE = """
<div>
  <h1 class="top-card-layout__title">Cloud Engineer</h1>
  <a class="apply-button"
     href="https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fjobs.lever.co%2Fbeta%2Fu-9">Apply</a>
</div>
"""

EMBEDDED_JSON_PAGE = """
<html><body><script>
{"companyApplyUrl":"https:\\/\\/jobs.ashbyhq.com\\/gamma\\/uuid-1","x":1}
</script></body></html>
"""

EASY_APPLY_PAGE = """
<div><h1 class="topcard__title">Platform Eng</h1>
<button data-tracking-control-name="easy-apply">Easy Apply</button></div>
"""


def _soup(text):
    return BeautifulSoup(text, "html.parser")


def test_parse_linkedin_dom_fields():
    d = job_fetch._parse_linkedin_dom(_soup(GUEST_FRAGMENT))
    assert d["title"] == "Senior SRE"
    assert d["company"] == "Acme Corp"
    assert d["location"] == "Raleigh, NC"
    assert "Run prod." in d["description"]


def test_apply_url_from_code_element():
    url = job_fetch._linkedin_apply_url(_soup(GUEST_FRAGMENT), GUEST_FRAGMENT)
    assert url == "https://boards.greenhouse.io/acme/jobs/777"


def test_apply_url_unwraps_safety_redirect_anchor():
    url = job_fetch._linkedin_apply_url(_soup(APPLY_ANCHOR_PAGE), APPLY_ANCHOR_PAGE)
    assert url == "https://jobs.lever.co/beta/u-9"


def test_apply_url_safety_redirect_decodes_exactly_once():
    page = ('<a class="apply-button" href="https://www.linkedin.com/safety/go/'
            '?url=https%3A%2F%2Fjobs.example.com%2Fapply%3Fref%3Da%2520b">Apply</a>')
    url = job_fetch._linkedin_apply_url(_soup(page), page)
    assert url == "https://jobs.example.com/apply?ref=a%20b"


def test_apply_url_from_embedded_json_models():
    url = job_fetch._linkedin_apply_url(_soup(EMBEDDED_JSON_PAGE), EMBEDDED_JSON_PAGE)
    assert url == "https://jobs.ashbyhq.com/gamma/uuid-1"


def test_apply_url_empty_for_easy_apply():
    assert job_fetch._linkedin_apply_url(_soup(EASY_APPLY_PAGE), EASY_APPLY_PAGE) == ""


def test_apply_url_rejects_linkedin_targets():
    page = '<a class="apply-button" href="https://www.linkedin.com/authwall">x</a>'
    assert job_fetch._linkedin_apply_url(_soup(page), page) == ""


CANONICAL_PAGE = """
<html><head><script type="application/ld+json">
{"@type":"JobPosting","title":"Senior SRE","datePosted":"2026-07-04",
 "hiringOrganization":{"name":"Acme Corp"},
 "jobLocation":{"address":{"addressLocality":"Raleigh","addressRegion":"NC"}},
 "description":"Run prod."}
</script></head><body></body></html>
"""

WALLED_PAGE = "<html><body><form class='login-form'>Sign in</form></body></html>"


def _fake_get(pages):
    """Return a _get stub serving canned bodies keyed by URL substring."""
    calls = []
    def get(url, referer=None, timeout=30):
        calls.append(url)
        for key, body in pages.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise AssertionError(f"unexpected fetch: {url}")
    get.calls = calls
    return get


def test_fetch_linkedin_job_canonical_plus_fragment(monkeypatch):
    monkeypatch.setattr(job_fetch, "_get", _fake_get({
        "/jobs/view/123/": CANONICAL_PAGE,
        "/jobs-guest/jobs/api/jobPosting/123": GUEST_FRAGMENT,
    }))
    d = job_fetch.fetch_linkedin_job("https://www.linkedin.com/jobs/view/123/")
    assert d["title"] == "Senior SRE"
    assert d["company"] == "Acme Corp"
    assert d["location"] == "Raleigh, NC"
    assert d["description"] == "Run prod."
    assert d["posted_at"] == "2026-07-04"
    assert d["apply_url"] == "https://boards.greenhouse.io/acme/jobs/777"
    assert d["canonical_url"] == "https://www.linkedin.com/jobs/view/123/"


def test_fetch_linkedin_job_walled_canonical_uses_fragment(monkeypatch):
    monkeypatch.setattr(job_fetch, "_get", _fake_get({
        "/jobs/view/123/": WALLED_PAGE,
        "/jobs-guest/jobs/api/jobPosting/123": GUEST_FRAGMENT,
    }))
    d = job_fetch.fetch_linkedin_job("https://www.linkedin.com/jobs/view/123/")
    assert d["title"] == "Senior SRE"
    assert d["apply_url"].endswith("/jobs/777")


def test_fetch_linkedin_job_canonical_error_fragment_rescues(monkeypatch):
    monkeypatch.setattr(job_fetch, "_get", _fake_get({
        "/jobs/view/123/": RuntimeError("HTTP 429"),
        "/jobs-guest/jobs/api/jobPosting/123": GUEST_FRAGMENT,
    }))
    d = job_fetch.fetch_linkedin_job("https://www.linkedin.com/jobs/view/123/")
    assert d["title"] == "Senior SRE"


def test_fetch_linkedin_job_both_sources_empty_raises(monkeypatch):
    monkeypatch.setattr(job_fetch, "_get", _fake_get({
        "/jobs/view/123/": WALLED_PAGE,
        "/jobs-guest/jobs/api/jobPosting/123": RuntimeError("HTTP 429"),
    }))
    with pytest.raises(FetchError):
        job_fetch.fetch_linkedin_job("https://www.linkedin.com/jobs/view/123/")


def test_resolve_url_supported_ats(monkeypatch):
    import job_ingest
    monkeypatch.setattr(job_ingest, "fetch_posting_meta", lambda parsed: {
        "title": "Staff SRE", "location": "Remote", "description": "JD"})
    r = job_fetch.resolve_url("https://boards.greenhouse.io/acme/jobs/42")
    assert (r["ats"], r["company"], r["ext_id"]) == ("greenhouse", "acme", "42")
    assert r["title"] == "Staff SRE"
    assert r["date_source"] == ""
    assert r["url"] == "https://boards.greenhouse.io/acme/jobs/42"


def test_resolve_url_linkedin_chains_to_ats(monkeypatch):
    import job_ingest
    monkeypatch.setattr(job_fetch, "fetch_linkedin_job", lambda url, timeout=30: {
        "title": "Senior SRE", "company": "Acme Corp", "location": "Raleigh, NC",
        "description": "li desc", "posted_at": "2026-07-04",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/777",
        "canonical_url": "https://www.linkedin.com/jobs/view/123/"})
    monkeypatch.setattr(job_ingest, "fetch_posting_meta", lambda parsed: {
        "title": "Senior SRE", "location": "Raleigh, NC",
        "description": "full ats desc"})
    r = job_fetch.resolve_url("https://www.linkedin.com/jobs/view/123/")
    assert (r["ats"], r["company"], r["ext_id"]) == ("greenhouse", "acme", "777")
    assert r["description"] == "full ats desc"
    assert r["url"] == "https://boards.greenhouse.io/acme/jobs/777"
    assert r["posted_at"] == "2026-07-04"
    assert r["date_source"] == "jsonld"


def test_resolve_url_linkedin_chain_fetch_fails_keeps_linkedin(monkeypatch):
    import job_ingest
    monkeypatch.setattr(job_fetch, "fetch_linkedin_job", lambda url, timeout=30: {
        "title": "Senior SRE", "company": "Acme Corp", "location": "Raleigh, NC",
        "description": "li desc", "posted_at": "",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/777",
        "canonical_url": "https://www.linkedin.com/jobs/view/123/"})
    def boom(parsed):
        raise RuntimeError("api down")
    monkeypatch.setattr(job_ingest, "fetch_posting_meta", boom)
    r = job_fetch.resolve_url("https://www.linkedin.com/jobs/view/123/")
    assert (r["ats"], r["company"], r["ext_id"]) == ("greenhouse", "acme", "777")
    assert r["description"] == "li desc"       # LinkedIn text kept as fallback


def test_resolve_url_linkedin_easy_apply_stays_linkedin(monkeypatch):
    monkeypatch.setattr(job_fetch, "fetch_linkedin_job", lambda url, timeout=30: {
        "title": "Platform Eng", "company": "Beta Labs Inc.", "location": "Remote",
        "description": "d", "posted_at": "2026-07-05", "apply_url": "",
        "canonical_url": "https://www.linkedin.com/jobs/view/9/"})
    r = job_fetch.resolve_url("https://www.linkedin.com/jobs/view/9/")
    assert (r["ats"], r["company"], r["ext_id"]) == ("linkedin", "beta-labs-inc", "9")
    assert r["url"] == "https://www.linkedin.com/jobs/view/9/"
    assert r["date_source"] == "jsonld"


def test_resolve_url_generic_jsonld(monkeypatch):
    monkeypatch.setattr(job_fetch, "_get_page", lambda url, timeout=30: JSONLD_PAGE)
    r = job_fetch.resolve_url("https://careers.acme.com/openings/staff-platform")
    assert r["ats"] == "manual"
    assert r["company"] == "acme-corp"
    assert r["title"] == "Staff Platform Engineer"
    assert r["posted_at"] == "2026-07-01"
    assert r["date_source"] == "jsonld"
    assert len(r["ext_id"]) == 10              # stable sha1 short id


def test_resolve_url_generic_no_jsonld_raises(monkeypatch):
    monkeypatch.setattr(job_fetch, "_get_page", lambda url, timeout=30: NO_POSTING_PAGE)
    with pytest.raises(FetchError):
        job_fetch.resolve_url("https://careers.acme.com/openings/x")


def test_resolve_url_generic_fetch_error_raises(monkeypatch):
    def boom(url, timeout=30):
        raise RuntimeError("403")
    monkeypatch.setattr(job_fetch, "_get_page", boom)
    with pytest.raises(FetchError):
        job_fetch.resolve_url("https://careers.acme.com/openings/x")


SEARCH_RESULTS_PAGE = """
<html><body><h1>1,000+ Flash Developer Jobs in United States</h1></body></html>
"""


def test_is_search_aggregate_title():
    assert job_fetch._is_search_aggregate_title(
        "1,000+ Flash Developer Jobs in United States")
    assert job_fetch._is_search_aggregate_title(
        "57,000+ Site Reliability Engineer Jobs in United States")
    assert not job_fetch._is_search_aggregate_title("Senior SRE")
    assert not job_fetch._is_search_aggregate_title("Staff Platform Engineer")
    assert not job_fetch._is_search_aggregate_title("")


def test_parse_linkedin_dom_rejects_search_aggregate_title():
    d = job_fetch._parse_linkedin_dom(_soup(SEARCH_RESULTS_PAGE))
    assert d["title"] == ""


def test_fetch_linkedin_job_walled_search_page_uses_fragment_title(monkeypatch):
    # Canonical page is a jobs-search results page (guest wall); the fragment
    # still carries the real posting. The aggregate title must not win.
    monkeypatch.setattr(job_fetch, "_get", _fake_get({
        "/jobs/view/123/": SEARCH_RESULTS_PAGE,
        "/jobs-guest/jobs/api/jobPosting/123": GUEST_FRAGMENT,
    }))
    d = job_fetch.fetch_linkedin_job("https://www.linkedin.com/jobs/view/123/")
    assert d["title"] == "Senior SRE"
    assert d["company"] == "Acme Corp"


def test_fetch_linkedin_job_search_page_and_dead_fragment_raises(monkeypatch):
    monkeypatch.setattr(job_fetch, "_get", _fake_get({
        "/jobs/view/123/": SEARCH_RESULTS_PAGE,
        "/jobs-guest/jobs/api/jobPosting/123": RuntimeError("HTTP 429"),
    }))
    with pytest.raises(job_fetch.FetchError):
        job_fetch.fetch_linkedin_job("https://www.linkedin.com/jobs/view/123/")


# --- company name on ATS-vendor-hosted careers sites -----------------------

AVATURE_STUB_PAGE = """
<html><head>
<meta property="og:site_name" content="One Call">
<meta property="og:title" content="Senior Manager Engineering">
<script type="application/ld+json">
{"@context":"https://schema.org/","@type":"JobPosting",
 "title":"Sr Mgr Engineering","datePosted":"2026-07-23"}
</script>
</head><body><p>Lead multiple engineering teams.</p></body></html>
"""


def test_site_name_reads_og_site_name():
    assert job_fetch._site_name(AVATURE_STUB_PAGE) == "One Call"


def test_site_name_absent_is_empty():
    assert job_fetch._site_name("<html><head></head></html>") == ""


def test_resolve_generic_prefers_og_site_name_over_vendor_host(monkeypatch):
    """An Avature stub has no hiringOrganization and a vendor host, so without
    og:site_name the company would come out as the ATS vendor."""
    monkeypatch.setattr(job_fetch, "_get_page", lambda url, timeout=None: AVATURE_STUB_PAGE)
    out = job_fetch._resolve_generic(
        "https://onecall.avature.net/careers/JobDetail/Sr-Mgr-Engineering/5773")
    assert out["company"] == "one-call"
    assert out["ats"] == "manual"


def test_host_company_slug_uses_tenant_on_ats_vendor_domain():
    import job_ingest
    assert job_ingest._host_company_slug(
        "https://onecall.avature.net/careers/JobDetail/x/5773") == "onecall"
    assert job_ingest._host_company_slug(
        "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/x") == "acme"
    assert job_ingest._host_company_slug(
        "https://cascademedical.icims.com/jobs/1234/job") == "cascademedical"


def test_host_company_slug_unchanged_for_own_domain():
    import job_ingest
    assert job_ingest._host_company_slug("https://apply.omnicell.com/jobs/1") == "omnicell"
    assert job_ingest._host_company_slug("https://careers.humana.com/job/9") == "humana"


def test_resolve_url_carries_a_posting_date_the_ats_supplies(monkeypatch):
    """The parsed-ATS branch used to hardcode posted_at="", which silently
    downgraded a Workday URL: the generic JSON-LD path it replaced DID read a
    date off the page, so parsing the URL traded a wrong location for a
    missing date. A meta dict without these keys still yields "" (see
    test_resolve_url_supported_ats), so the other four ATSes are unaffected."""
    import job_ingest
    monkeypatch.setattr(job_ingest, "fetch_posting_meta", lambda parsed: {
        "title": "Sr. Manager, SRE", "location": "Remote - United States",
        "description": "JD", "posted_at": "2026-09-01",
        "date_source": "workday:postedOn~"})
    r = job_fetch.resolve_url(
        "https://pennmutual.wd1.myworkdayjobs.com/_penn-careers"
        "/job/Remote---United-States/Sr-Manager_R-100661")
    assert r["ats"] == "workday"
    assert r["location"] == "Remote - United States"
    assert r["posted_at"] == "2026-09-01"
    assert r["date_source"] == "workday:postedOn~"
