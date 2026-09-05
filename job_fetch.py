"""Fetch a single job posting by URL.

Covers LinkedIn (public logged-out guest view), plus any careers page that
embeds schema.org JobPosting JSON-LD. resolve_url() also routes supported ATS
URLs through job_ingest's existing matchers so every consumer has one entry
point. Adapted from pushcv-cli (https://github.com/notnotparas/pushcv-cli, MIT).

Hard-rule compliance: LinkedIn access is the logged-out guest view only (no
login, no cookies), one posting per call, always user-initiated. This module
must never gain a search or discovery entry point. Side-effect-free: returns
dicts, no DB access, no printing.
"""
import html as html_mod
import json
import re
from urllib.parse import urlsplit, parse_qs

from bs4 import BeautifulSoup, Tag
from curl_cffi import requests as curl_requests


DEFAULT_TIMEOUT = 30


class FetchError(Exception):
    """A posting could not be fetched or parsed. str(e) is human-readable."""


def is_linkedin_url(url):
    host = (urlsplit(url).hostname or "").lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def linkedin_job_id(url):
    """Extract the numeric LinkedIn job id from any LinkedIn job URL.

    Search/collection URLs carry it in ?currentJobId=; view URLs carry it as
    the trailing digits of /jobs/view/<slug-or-id>.
    """
    m = re.search(r"[?&]currentJobId=(\d+)", url)
    if m is None:
        m = re.search(r"/jobs/view/(?:[^/?#]*?-)?(\d+)", url)
    if m is None:
        raise FetchError(f"no LinkedIn job id found in {url}")
    return m.group(1)


def _clean(value):
    """Collapse whitespace; return '' for None/empty."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _strip_tags(value):
    """HTML description to plain text, tags and entities removed."""
    if not isinstance(value, str) or not value:
        return ""
    text = BeautifulSoup(value, "html.parser").get_text(separator=" ")
    # Remove spaces before common punctuation
    text = re.sub(r' ([.,:;!?])', r'\1', text)
    return _clean(text)


def _iter_json_ld(soup):
    """Every successfully decoded application/ld+json payload on the page."""
    payloads = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            payloads.append(json.loads(raw))
        except (json.JSONDecodeError, ValueError):
            continue  # malformed block, skip it rather than fail the parse
    return payloads


def _find_job_posting(payloads):
    """First JobPosting node across payloads (bare, list, or @graph)."""
    def is_job(node):
        if not isinstance(node, dict):
            return False
        t = node.get("@type")
        return "JobPosting" in t if isinstance(t, list) else t == "JobPosting"

    def walk(node):
        if is_job(node):
            return node
        if isinstance(node, dict):
            children = node.get("@graph", []) if "@graph" in node else node.values()
            for value in children:
                found = walk(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found is not None:
                    return found
        return None

    for payload in payloads:
        found = walk(payload)
        if found is not None:
            return found
    return None


def _org_name(hiring_org):
    if isinstance(hiring_org, list):
        hiring_org = hiring_org[0] if hiring_org else None
    if isinstance(hiring_org, dict):
        return _clean(hiring_org.get("name"))
    return ""


def _format_address(address):
    if isinstance(address, str):
        return _clean(address)
    if not isinstance(address, dict):
        return ""
    parts = [address.get("addressLocality"), address.get("addressRegion"),
             address.get("addressCountry")]
    return _clean(", ".join(p for p in parts if isinstance(p, str) and p.strip()))


def _job_location(job_location):
    if isinstance(job_location, list):
        job_location = job_location[0] if job_location else None
    if isinstance(job_location, dict):
        return _format_address(job_location.get("address"))
    return ""


def parse_jsonld_posting(html_text):
    """Parse a schema.org JobPosting from page HTML.

    Returns {title, company, location, description, posted_at} (strings,
    possibly empty) or None when the page has no JobPosting node.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    job = _find_job_posting(_iter_json_ld(soup))
    if job is None:
        return None
    return {
        "title": _clean(job.get("title")),
        "company": _org_name(job.get("hiringOrganization")),
        "location": _job_location(job.get("jobLocation")),
        "description": _strip_tags(job.get("description")),
        "posted_at": _clean(job.get("datePosted")),
    }


# CSS classes LinkedIn uses on the external apply anchor across guest variants.
_APPLY_BUTTON_SELECTOR = (
    "a.apply-button, a[class*='apply-button'], "
    "a.sign-up-modal__direct-apply-on-company-site, "
    "a[data-tracking-control-name*='apply']"
)

# Off-site apply URLs also appear inside serialized JSON models in the page.
_APPLY_KEY_RE = re.compile(
    r'"(?:companyApplyUrl|applyUrl|companyApplyURL|easyApplyUrl)"'
    r'\s*:\s*"((?:[^"\\]|\\.)*)"')


def _select_text(soup, *selectors):
    for selector in selectors:
        el = soup.select_one(selector)
        if el is not None:
            text = _clean(el.get_text(separator=" "))
            if text:
                return text
    return ""


# LinkedIn answers a guest-walled or expired posting id with a jobs-search
# results page (HTTP 200, not a 404). Its <h1> is an aggregate count, e.g.
# "1,000+ Flash Developer Jobs in United States", never a real posting title.
# Rejecting it stops the canonical page from poisoning the title so the guest
# fragment's real posting can fill in (or, if that is also empty, the fetch
# fails cleanly).
_SEARCH_AGGREGATE_TITLE_RE = re.compile(r"^[\d,]+\+?\s+.+\bjobs\s+in\b",
                                        re.IGNORECASE)


def _is_search_aggregate_title(title):
    return bool(title) and bool(_SEARCH_AGGREGATE_TITLE_RE.match(title))


def _parse_linkedin_dom(soup):
    """Posting fields from LinkedIn guest markup (page or jobs-guest fragment)."""
    title = _select_text(soup, ".top-card-layout__title", ".topcard__title",
                         "h1.topcard__title", "h1")
    if _is_search_aggregate_title(title):
        title = ""
    company = _select_text(soup, "a.topcard__org-name-link",
                           ".topcard__org-name-link",
                           ".top-card-layout__second-subline a")
    location = _select_text(soup, ".topcard__flavor--bullet",
                            ".top-card-layout__second-subline .topcard__flavor--bullet")
    desc_el = (soup.select_one(".show-more-less-html__markup")
               or soup.select_one(".description__text"))
    if desc_el:
        text = desc_el.get_text(separator=" ")
        text = re.sub(r' ([.,:;!?])', r'\1', text)
        description = _clean(text)
    else:
        description = ""
    return {"title": title, "company": company, "location": location,
            "description": description}


def _unwrap_apply_url(url):
    """Unwrap LinkedIn's /safety/go/?url=<encoded> redirect to its target."""
    if not url:
        return ""
    split = urlsplit(url)
    host = (split.hostname or "").lower()
    if (host == "linkedin.com" or host.endswith(".linkedin.com")) \
            and "/safety/go" in split.path:
        target = parse_qs(split.query).get("url", [""])[0]
        if target:
            return target
    return url


def _is_external_apply(url):
    """True only for an http(s) URL that leaves linkedin.com."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return False
    host = (urlsplit(url).hostname or "").lower()
    return bool(host) and host != "linkedin.com" and not host.endswith(".linkedin.com")


def _apply_url_from_code(soup):
    """The off-site apply URL LinkedIn stashes in <code id="applyUrl">.

    The URL is a JSON string literal inside an HTML comment, e.g.
    <code id="applyUrl"><!--"https:\\/\\/co.com\\/x"--></code>.
    """
    code = soup.find("code", id="applyUrl")
    if not isinstance(code, Tag):
        return ""
    inner = code.decode_contents().strip()
    inner = re.sub(r"^<!--", "", inner)
    inner = re.sub(r"-->$", "", inner).strip()
    if not inner:
        return ""
    try:
        decoded = json.loads(inner)
        if isinstance(decoded, str):
            inner = decoded
    except (json.JSONDecodeError, ValueError):
        inner = inner.strip('"')
    return _clean(html_mod.unescape(inner))


def _apply_url_from_html(html_text):
    """Scan raw HTML for an off-site apply URL inside embedded JSON models."""
    for m in _APPLY_KEY_RE.finditer(html_text):
        raw = m.group(1)
        try:
            url = json.loads(f'"{raw}"')  # restores \/ escapes
        except (json.JSONDecodeError, ValueError):
            url = raw.replace("\\/", "/")
        url = _unwrap_apply_url(_clean(html_mod.unescape(url)))
        if _is_external_apply(url):
            return url
    for m in re.finditer(r"/safety/go/?\?[^\"'<>\\ ]*?url=([^\"'<>&\\ ]+)", html_text):
        url = _unwrap_apply_url(
            f"https://www.linkedin.com/safety/go/?url={m.group(1)}")
        if _is_external_apply(url):
            return url
    return ""


def _linkedin_apply_url(soup, html_text):
    """The external apply URL, or '' for Easy Apply / gated postings.

    Prefers the authoritative <code id="applyUrl"> payload, then an
    apply-button anchor, then embedded JSON models. linkedin.com targets are
    login gates, never the real employer, so they are rejected.
    """
    from_code = _unwrap_apply_url(_apply_url_from_code(soup))
    if _is_external_apply(from_code):
        return from_code
    anchor = soup.select_one(_APPLY_BUTTON_SELECTOR)
    if isinstance(anchor, Tag):
        href = anchor.get("href")
        unwrapped = _unwrap_apply_url(href) if isinstance(href, str) else ""
        if _is_external_apply(unwrapped):
            return _clean(unwrapped)
    return _apply_url_from_html(html_text)


# LinkedIn bounces desktop clients toward auth walls; presenting as Mobile
# Safari on iOS reaches the public server-rendered guest view. curl_cffi sets
# the matching TLS fingerprint, the headers complete the disguise.
LINKEDIN_IMPERSONATE = "safari_ios"
LINKEDIN_GUEST_API = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
IPHONE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_3_1 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 "
                   "Mobile/15E148 Safari/604.1"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _get(url, referer=None, timeout=DEFAULT_TIMEOUT):
    """Fetch a LinkedIn URL as Mobile Safari. Tests monkeypatch this."""
    headers = dict(IPHONE_HEADERS)
    if referer:
        headers["Referer"] = referer
    r = curl_requests.get(url, impersonate=LINKEDIN_IMPERSONATE,
                          headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def fetch_linkedin_job(url, timeout=DEFAULT_TIMEOUT):
    """Fetch one LinkedIn posting via the public guest view.

    Tries the canonical /jobs/view/{id}/ page (JSON-LD when not walled), then
    the jobs-guest fragment endpoint (server-rendered markup, not behind the
    auth wall), filling gaps from each. Raises FetchError when neither source
    yields a title.
    """
    job_id = linkedin_job_id(url)
    canonical_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
    fields = {"title": "", "company": "", "location": "", "description": "",
              "apply_url": "", "posted_at": ""}

    def absorb(html_text):
        soup = BeautifulSoup(html_text, "html.parser")
        jsonld = parse_jsonld_posting(html_text) or {}
        dom = _parse_linkedin_dom(soup)
        for key in ("title", "company", "location", "description"):
            value = jsonld.get(key) or dom.get(key, "")
            if value and not fields[key]:
                fields[key] = value
        if jsonld.get("posted_at") and not fields["posted_at"]:
            fields["posted_at"] = jsonld["posted_at"]
        if not fields["apply_url"]:
            fields["apply_url"] = _linkedin_apply_url(soup, html_text)

    # LinkedIn intermittently answers the canonical page with 429/999 for
    # guests; remember the failure, the fragment endpoint often still works.
    canonical_error = None
    try:
        absorb(_get(canonical_url, timeout=timeout))
    except Exception as exc:
        canonical_error = exc

    if not fields["title"] or not fields["description"] or not fields["apply_url"]:
        try:
            body = _get(LINKEDIN_GUEST_API.format(job_id=job_id),
                        referer=canonical_url, timeout=timeout)
            if body and body.strip():
                absorb(body)
        except Exception:
            pass  # only fatal if the canonical page also gave nothing

    if not fields["title"]:
        reason = str(canonical_error) if canonical_error else \
            "LinkedIn returned no posting data (auth wall)"
        raise FetchError(f"could not read LinkedIn posting {job_id}: {reason}")

    fields["canonical_url"] = canonical_url
    return fields


def _get_page(url, timeout=DEFAULT_TIMEOUT):
    """Fetch a non-LinkedIn careers page as Chrome. Tests monkeypatch this."""
    r = curl_requests.get(url, impersonate="chrome", timeout=timeout)
    r.raise_for_status()
    return r.text


def _slugify(name):
    """Company display name to a slug: 'Beta Labs Inc.' -> 'beta-labs-inc'."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def _site_name(html_text):
    """Company display name from og:site_name.

    Some ATS-hosted careers sites emit a JobPosting stub with no
    hiringOrganization (Avature ships title and datePosted only), and the host
    is the vendor's, not the company's. og:site_name carries the real name.
    """
    el = BeautifulSoup(html_text, "html.parser").find(
        "meta", attrs={"property": "og:site_name"})
    return (el.get("content") or "").strip() if el else ""


def resolve_url(url, timeout=DEFAULT_TIMEOUT):
    """Resolve any posting URL to one normalized dict.

    Order: supported ATS URL (job_ingest matchers), LinkedIn guest view (with
    chain-scrape to the canonical ATS posting when the apply URL reveals one,
    so the uid dedupes against scan results), then generic JSON-LD JobPosting.
    Returns {ats, company, ext_id, title, location, description, url,
    posted_at, date_source} or raises FetchError.
    """
    # Lazy import: job_ingest imports this module at its top level, so a
    # top-level import here would be a cycle.
    import job_ingest

    parsed = job_ingest.parse_posting_url(url)
    if parsed:
        try:
            meta = job_ingest.fetch_posting_meta(parsed)
        except Exception as e:
            raise FetchError(f"ATS fetch failed for {url}: {e}")
        return {"ats": parsed["ats"], "company": parsed["company"],
                "ext_id": parsed["ext_id"], "title": meta.get("title", ""),
                "location": meta.get("location", ""),
                "description": meta.get("description", ""),
                "url": url,
                # Most ATS metadata fetchers report no date and default to "",
                # exactly as before. Workday's does report one, and hardcoding
                # "" here threw it away: parsing the URL would then have traded
                # the generic path's wrong location for a missing date.
                "posted_at": meta.get("posted_at", ""),
                "date_source": meta.get("date_source", "")}

    if is_linkedin_url(url):
        return _resolve_linkedin(url, timeout=timeout)
    return _resolve_generic(url, timeout=timeout)


def _resolve_linkedin(url, timeout=DEFAULT_TIMEOUT):
    import job_ingest

    li = fetch_linkedin_job(url, timeout=timeout)
    posted_at = li["posted_at"]
    date_source = "jsonld" if posted_at else ""

    # Chain-scrape: an apply URL on a supported ATS makes that posting the
    # canonical record (same uid the daily scan would produce).
    chained = job_ingest.parse_posting_url(li["apply_url"]) if li["apply_url"] else None
    if chained:
        try:
            meta = job_ingest.fetch_posting_meta(chained)
        except Exception:
            meta = {}  # ATS unreachable: keep the LinkedIn text
        return {"ats": chained["ats"], "company": chained["company"],
                "ext_id": chained["ext_id"],
                "title": meta.get("title") or li["title"],
                "location": meta.get("location") or li["location"],
                "description": meta.get("description") or li["description"],
                "url": li["apply_url"], "posted_at": posted_at,
                "date_source": date_source}

    company = _slugify(li["company"]) or "linkedin"
    return {"ats": "linkedin", "company": company,
            "ext_id": linkedin_job_id(url), "title": li["title"],
            "location": li["location"], "description": li["description"],
            "url": li["canonical_url"], "posted_at": posted_at,
            "date_source": date_source}


def _resolve_generic(url, timeout=DEFAULT_TIMEOUT):
    import job_ingest

    try:
        html_text = _get_page(url, timeout=timeout)
    except Exception as e:
        raise FetchError(f"could not fetch {url}: {e}")
    posting = parse_jsonld_posting(html_text)
    if posting is None or not posting["title"]:
        raise FetchError(
            f"no JobPosting metadata on {url} (not an ATS URL, not LinkedIn, "
            "and the page embeds no schema.org JobPosting)")
    company = (_slugify(posting["company"])
               or _slugify(_site_name(html_text))
               or job_ingest._host_company_slug(url) or "manual")
    return {"ats": "manual", "company": company,
            "ext_id": job_ingest._manual_ext_id(url),
            "title": posting["title"], "location": posting["location"],
            "description": posting["description"], "url": url,
            "posted_at": posting["posted_at"],
            "date_source": "jsonld" if posting["posted_at"] else ""}
