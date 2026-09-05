#!/usr/bin/env python3
"""
job_monitor.py - Read-only job discovery CLI.

Polls public ATS job-board APIs for a target-company list, filters by title
and location, diffs against the previous run, and reports only NEW matches.

Discovery only. This never applies to anything. It reads the same public
endpoints a company's own careers page calls from the browser.

Supported ATS (public, no auth):
  - Greenhouse       boards-api.greenhouse.io/v1/boards/{slug}/jobs
  - Lever            api.lever.co/v0/postings/{slug}?mode=json
  - Ashby            api.ashbyhq.com/posting-api/job-board/{slug}
  - SmartRecruiters  api.smartrecruiters.com/v1/companies/{slug}/postings

Not publicly fetchable without partner credentials:
  - iCIMS, Workday   -> listed as "manual" so you check them by hand or via
                        the Google Custom Search route.

Usage:
  python job_monitor.py --dry-run             # print matches, write nothing
  python job_monitor.py --first-run           # seed state, suppress notifications
  python job_monitor.py                        # normal run: diff + report (+ Discord)
  python job_monitor.py -c companies_test.yaml --dry-run
  python job_monitor.py --no-discord
  python job_monitor.py --state ./mystate.json

Config defaults to ./companies.yaml. State defaults to a per-user data dir
(see resolve_state_path). Both are overridable so this can be wrapped by an
app or skill later without code changes.

Env:
  JOB_CONTACT_EMAIL   contact address advertised in the outbound User-Agent.
                      Defaults to a neutral placeholder; set it to a real
                      mailbox you read so an ATS operator can reach you.

The fetch/filter/diff functions are import-safe: `from job_monitor import
run_scan` returns structured results with no side effects, which is the entry
point a future skill or GUI would call.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent


def resolve_state_path(override=None):
    """Where to keep seen-jobs state.

    Order of preference:
      1. explicit --state override
      2. JOB_MONITOR_STATE env var
      3. platform user-data dir (~/.local/share or macOS equivalent)
      4. fallback: next to the script
    A per-user data dir (not next to the script) is the right home once this
    becomes an installed app or skill.
    """
    if override:
        return Path(override).expanduser()
    env = os.environ.get("JOB_MONITOR_STATE")
    if env:
        return Path(env).expanduser()
    # XDG-style on Linux, Application Support on macOS, else home.
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "job-monitor"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home())) / "job-monitor"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "job-monitor"
    base.mkdir(parents=True, exist_ok=True)
    return base / "seen_jobs.json"

# Be a polite client. Identify yourself and don't hammer hosts.
# The contact address is configurable so a real, monitored mailbox can be
# advertised without hardcoding one in the repo. The default is a placeholder,
# never a real address.
CONTACT_EMAIL = os.environ.get("JOB_CONTACT_EMAIL", "job-monitor@example.invalid")
SESSION = requests.Session()
SESSION.headers.update(
    {"User-Agent": f"personal-job-monitor/1.0 (individual job seeker; contact: {CONTACT_EMAIL})"}
)
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_CALLS = 1.5  # seconds, deliberate politeness delay

# Location tokens that mean "remote-eligible" (used for the location_type tag and
# included in companies.yaml location_terms). "united states" is a documented
# heuristic: ATS listings that name the whole country are almost always remote-US.
REMOTE_TERMS = ["remote", "anywhere", "distributed", "united states"]

# Explicit remote signals in a LOCATION field (distinct from the whole-country
# heuristic). A bare "remote"/"distributed"/"anywhere" location is remote-US.
REMOTE_TOKEN_TERMS = ["remote", "anywhere", "distributed"]
REMOTE_TOKEN_PATS = [re.compile(r"\b" + re.escape(t) + r"\b", re.IGNORECASE)
                     for t in REMOTE_TOKEN_TERMS]
# The bare word "remote" in a TITLE still counts (some ATS put it there), but
# other remote tokens in a title (e.g. "distributed storage") must not.
REMOTE_WORD_RE = re.compile(r"\bremote\b", re.IGNORECASE)
# Whole-country US markers. Used to detect the "City, State, United States"
# onsite case: country alone reads as remote, but country + a specific metro
# (that isn't in commuting radius) is an onsite role, not remote-eligible.
COUNTRY_TERMS = ["united states", "usa", "u.s."]  # "u.s." also covers "U.S.A."


def _word_bounded(term):
    """Wrap a term in word boundaries, but only on a side that ends in an
    alphanumeric. A trailing \\b after a term ending in punctuation (e.g.
    'u.s.') would fail to match 'U.S.' at end-of-string or before a comma, so
    that boundary is omitted for dotted terms."""
    pat = re.escape(term)
    if term[:1].isalnum():
        pat = r"\b" + pat
    if term[-1:].isalnum():
        pat = pat + r"\b"
    return pat


COUNTRY_PATS = [re.compile(_word_bounded(t), re.IGNORECASE) for t in COUNTRY_TERMS]
COUNTRY_RE = re.compile("|".join(_word_bounded(t) for t in COUNTRY_TERMS),
                        re.IGNORECASE)
# US state names + codes. Used to tell an onsite metro ("San Francisco,
# California, United States") apart from a multi-region remote listing
# ("United States, Canada"): the former pins a specific US state, the latter
# does not. "New Mexico" precedes "Mexico" in the alternation so it wins.
US_STATES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
]
US_STATE_CODES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
]
US_STATE_NAME_RE = re.compile(
    "|".join(r"\b" + re.escape(s) + r"\b" for s in US_STATES), re.IGNORECASE)
US_STATE_CODE_SET = {c.upper() for c in US_STATE_CODES}
# A two-letter code is only read as a state when it sits in "City, ST" position
# (comma + optional space + two letters at a word boundary). This matches codes
# case-insensitively ("Austin, tx") without ever catching English words like
# "or"/"in"/"me" that are never comma-prefixed in a location string.
_CODE_AFTER_COMMA_RE = re.compile(r",\s*([A-Za-z]{2})\b")


def _names_us_state(text):
    if US_STATE_NAME_RE.search(text):
        return True
    return any(m.group(1).upper() in US_STATE_CODE_SET
               for m in _CODE_AFTER_COMMA_RE.finditer(text))


# A whole-country "remote" role can still require residency in a specific US
# time zone or sales territory. That requirement shows up either in the title
# (a territory suffix like "... - Central") or only in the JD body ("reside
# within the Central Time Zone"). The operator is in US Eastern, so a role that names a
# non-Eastern zone and not Eastern is out of reach and should be held for a JD
# check ('verify') rather than surfaced as reachable-remote.
_ZONE_WORD_RE = re.compile(r"\b(eastern|central|mountain|pacific)\b", re.IGNORECASE)
# A zone word only counts as a residency signal when it is tied to a time zone,
# in one of two unambiguous forms. Anchoring on these (not the bare word "time",
# which also lives in "full-time") avoids false positives like "central role".
#   1. "<zone> Time" directly: "Central Time", "Pacific Standard Time".
_ZONE_TIME_RE = re.compile(
    r"\b(eastern|central|mountain|pacific)\s+"
    r"(?:(?:standard|daylight|prevailing)\s+)?time\b", re.IGNORECASE)
#   2. the phrase "time zone(s)" / "timezone", which can lead or trail a list
#      ("time zones: Central, Pacific" / "Eastern, Central, Pacific Time Zones").
#      Zone words within ~45 chars of the phrase are collected.
_TZ_PHRASE_RE = re.compile(r"\btime\s*zones?\b|\btimezone\b", re.IGNORECASE)
# Timezone abbreviations are unambiguous enough to count on their own, with one
# exception: lowercase "est." (for "established"/"estimated") is common in JD
# prose, and matching it would inject a phantom Eastern zone that defeats the
# filter. So EST/EDT are matched uppercase-only; the rest have no English
# homograph and stay case-insensitive.
_TZ_ABBR = {"cst": "central", "cdt": "central",
            "mst": "mountain", "mdt": "mountain", "pst": "pacific", "pdt": "pacific"}
_TZ_ABBR_RE = re.compile(r"\b(" + "|".join(_TZ_ABBR) + r")\b", re.IGNORECASE)
_EASTERN_ABBR_RE = re.compile(r"\b(?:EST|EDT)\b")  # case-sensitive on purpose
# A title territory suffix: "Growth - Central", "New Logo -West". Anchored to the
# end of the title (with an optional "US"/"Region"/"Coast" qualifier) so a mid-
# title "- Central Platform" is not mistaken for a territory. West/East read as
# the coastal zone they front.
_REGION_SUFFIX_RE = re.compile(
    r"-\s*(east|central|mountain|pacific|west)\b"
    r"(?:\s+(?:us|usa|region|coast|territory))?\s*$", re.IGNORECASE)
_COAST_RE = re.compile(r"\b(east|west)\s+coast\b", re.IGNORECASE)
_SUFFIX_ZONE = {"east": "eastern", "central": "central", "mountain": "mountain",
                "pacific": "pacific", "west": "pacific"}
_COAST_ZONE = {"east": "eastern", "west": "pacific"}


def _named_zones(text, include_suffix=False):
    """Set of US time-zone names a text flags as a residency signal.

    `include_suffix` enables the title territory suffix ("... - Central"). It is
    off for JD body prose, where a bare "- central" (as in "collaboration -
    central to the role") is not a residency signal and would over-quarantine.
    """
    if not text:
        return set()
    zones = set()
    for m in _ZONE_TIME_RE.finditer(text):
        zones.add(m.group(1).lower())
    for tm in _TZ_PHRASE_RE.finditer(text):
        # Collect zone words flanking a "time zone(s)" label (covers lists where
        # the shared label leads or trails the enumeration).
        window = text[max(0, tm.start() - 45): tm.end() + 45]
        for zm in _ZONE_WORD_RE.finditer(window):
            zones.add(zm.group(1).lower())
    for m in _TZ_ABBR_RE.finditer(text):
        zones.add(_TZ_ABBR[m.group(1).lower()])
    if _EASTERN_ABBR_RE.search(text):
        zones.add("eastern")
    for m in _COAST_RE.finditer(text):
        zones.add(_COAST_ZONE[m.group(1).lower()])
    if include_suffix:
        for m in _REGION_SUFFIX_RE.finditer(text):
            zones.add(_SUFFIX_ZONE[m.group(1).lower()])
    return zones


def residency_excludes_eastern(text, *, in_title=False):
    """True when text pins one or more non-Eastern US zones and not Eastern.

    Used on both the title and the fetched JD body. Eastern named anywhere (or
    no zone at all) means the operator is not excluded. Fails open: an ambiguous mention
    biases toward keeping the role visible. Pass in_title=True to also read the
    title territory suffix; leave it off for prose to avoid false positives.
    """
    zones = _named_zones(text, include_suffix=in_title)
    return bool(zones - {"eastern"}) and "eastern" not in zones

# Roles based outside the US. A posting whose LOCATION names one of these is
# dropped even if its title says "remote" (those are remote-within-that-country,
# not US-eligible), unless the location also explicitly names the US (US_MARKERS).
FOREIGN_MARKERS = [
    "ireland", "united kingdom", "uk", "england", "scotland", "wales",
    "germany", "france", "spain", "italy", "netherlands", "belgium",
    "portugal", "poland", "romania", "sweden", "norway", "denmark", "finland",
    "switzerland", "austria", "czechia", "czech republic", "hungary", "greece",
    "canada", "australia", "new zealand", "india", "pakistan", "singapore",
    "malaysia", "indonesia", "philippines", "china", "hong kong", "taiwan",
    "japan", "south korea", "brazil", "argentina", "chile", "colombia",
    "mexico", "costa rica", "israel", "turkey", "egypt", "south africa",
    "nigeria", "kenya", "uae", "united arab emirates",
    # Multi-country regions. A global corpus posts plenty of roles whose
    # whole location field is the region ("Europe, Remote"), naming no
    # country and no city, so nothing else here catches them.
    "emea", "apac", "latam", "anz", "europe", "european",
    # Major offshore hiring hubs that show up as "Remote (City)" without a
    # country name. Only unambiguous ones (no US-city collisions like Dublin CA,
    # Vancouver WA, London KY, San Jose CR).
    "buenos aires", "sao paulo", "são paulo", "bengaluru", "bangalore",
    "hyderabad", "pune", "chennai", "gurgaon", "gurugram", "noida",
    "tel aviv", "krakow", "kraków", "warsaw", "wroclaw", "wrocław",
]
US_MARKERS = ["united states", "usa", "us", "new mexico"]

# "Americas" contains the US, so a listing like "Remote - Americas and Europe"
# IS US-eligible and adding "europe" to FOREIGN_MARKERS had started dropping
# it. But "Americas" is weaker evidence than naming the country: it was first
# added straight to US_MARKERS, and because matches() short circuits the
# foreign guard the moment any US marker appears, that let
# "Remote - Americas (Brazil, Mexico, Argentina)" through. Worse than the
# regression it fixed.
#
# So it vouches for US eligibility only when nothing sharper contradicts it:
# no specific foreign country or city, and no LATAM, which names the Americas
# MINUS the US and is therefore evidence against rather than for.
AMERICAS_RE = re.compile(r"\bamericas\b", re.IGNORECASE)
LATAM_RE = re.compile(r"\blatam\b", re.IGNORECASE)
# The region words among FOREIGN_MARKERS. A region does not name a country, so
# it is not sharp enough to override "Americas"; a country or a city is.
REGION_MARKERS = {"emea", "apac", "latam", "anz", "europe", "european"}
FOREIGN_PLACE_PATS = [re.compile(r"\b" + re.escape(m) + r"\b", re.IGNORECASE)
                      for m in FOREIGN_MARKERS if m not in REGION_MARKERS]


def americas_implies_us(loc):
    """True when a bare "Americas" in `loc` is fair evidence of US eligibility."""
    if not AMERICAS_RE.search(loc) or LATAM_RE.search(loc):
        return False
    return not any(p.search(loc) for p in FOREIGN_PLACE_PATS)
FOREIGN_PATS = [re.compile(r"\b" + re.escape(m) + r"\b", re.IGNORECASE) for m in FOREIGN_MARKERS]
US_PATS = [re.compile(r"\b" + re.escape(m) + r"\b", re.IGNORECASE) for m in US_MARKERS]


# --------------------------------------------------------------------------
# ATS fetchers. Each returns a list of normalized dicts:
#   {id, title, location, url, company, ats, posted_at, date_source}
# posted_at is an ISO8601 string (or "" if unavailable). date_source records
# which field it came from, so downstream code can be honest about whether the
# age is a true posting date or only an approximation.
# --------------------------------------------------------------------------

def _epoch_ms_to_iso(ms):
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    except (ValueError, TypeError, OSError):
        return ""


def _strip_html(html):
    """HTML to plain text, for descriptions carried in a scan response.

    Deliberately a local copy of job_generate._strip_html rather than an import:
    job_generate imports this module for its SESSION, so importing it back would
    be a cycle. Keep the two in step if either changes.
    """
    if not html:
        return ""
    text = re.sub(r"<\s*br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"</\s*(p|li|div|h\d)\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&nbsp;", " ").replace("&#39;", "'"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_greenhouse(slug):
    # content=true returns every posting's full description in the SAME request,
    # at no extra round trip. Without it the description column stays empty, and
    # fit.score's content markers (coding bar, sales role, exclude terms) can
    # never fire during ranking: the haystack is title and location only.
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        # The board listing only carries updated_at, which changes on any edit,
        # so it is a freshness approximation, not a true posting date. The real
        # first_published is fetched later, per-candidate, in freshness.py.
        out.append({
            "id": str(j.get("id")),
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "company": slug,
            "ats": "greenhouse",
            "posted_at": j.get("updated_at", "") or "",
            "date_source": "greenhouse:updated_at~",  # ~ = approximate
            "description": _strip_html(j.get("content", "")),
        })
    return out


def fetch_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        out.append({
            "id": str(j.get("id")),
            "title": j.get("text", ""),
            "location": cats.get("location", ""),
            "url": j.get("hostedUrl", ""),
            "company": slug,
            "ats": "lever",
            "posted_at": _epoch_ms_to_iso(j.get("createdAt")),
            "date_source": "lever:createdAt",
        })
    return out


def fetch_ashby(slug):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "id": str(j.get("id") or j.get("jobId") or j.get("title")),
            "title": j.get("title", ""),
            "location": j.get("location", "") or j.get("locationName", ""),
            "url": j.get("jobUrl", "") or j.get("applyUrl", ""),
            "company": slug,
            "ats": "ashby",
            "posted_at": j.get("publishedAt", "") or "",
            "date_source": "ashby:publishedAt",
        })
    return out


def fetch_smartrecruiters(slug):
    # Public posting API. Paginated; 100 per page is the max.
    out = []
    offset = 0
    while True:
        url = (
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
            f"?limit=100&offset={offset}"
        )
        r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        content = data.get("content", [])
        for j in content:
            loc = j.get("location") or {}
            loc_str = ", ".join(
                p for p in [loc.get("city"), loc.get("region"), loc.get("country")] if p
            )
            if loc.get("remote"):
                loc_str = (loc_str + " (remote)").strip()
            # `ref` may be a dict with a jobAd URL, or a plain string URL,
            # depending on the company's SmartRecruiters config. Handle both.
            ref = j.get("ref")
            if isinstance(ref, dict):
                ref_url = ref.get("jobAd", "")
            elif isinstance(ref, str):
                ref_url = ref
            else:
                ref_url = ""
            out.append({
                "id": str(j.get("id")),
                "title": j.get("name", ""),
                "location": loc_str,
                "url": ref_url or f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                "company": slug,
                "ats": "smartrecruiters",
                "posted_at": j.get("releasedDate", "") or "",
                "date_source": "smartrecruiters:releasedDate",
            })
        total = data.get("totalFound", 0)
        offset += len(content)
        if offset >= total or not content:
            break
        time.sleep(SLEEP_BETWEEN_CALLS)
    return out


_WORKDAY_DAYS_RE = re.compile(r"(\d+)\+?\s+days?\s+ago", re.IGNORECASE)


def _workday_relative_date(posted_on):
    """Turn Workday's relative postedOn text into an approximate ISO date.

    Workday gives "Posted Today" / "Posted Yesterday" / "Posted N Days Ago"
    (and "N+ Days Ago"), not a real timestamp, so the result is approximate
    (date_source carries a trailing ~). Returns "" when unparseable.
    """
    if not posted_on:
        return ""
    text = posted_on.lower()
    today = datetime.now(timezone.utc).date()
    if "today" in text:
        return today.isoformat()
    if "yesterday" in text:
        return (today - timedelta(days=1)).isoformat()
    m = _WORKDAY_DAYS_RE.search(text)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    return ""


def _workday_primary_location(external_path):
    """Derive a primary location from the externalPath /job/<Loc>/<slug>.

    Used when locationsText is empty or a summary like "2 Locations". Dashes
    in the path segment become spaces ("Atlanta-GA" -> "Atlanta GA"). v1
    limitation: a multi-location posting filters on its primary location only.
    """
    parts = [p for p in (external_path or "").split("/") if p]
    if "job" in parts:
        i = parts.index("job")
        if i + 1 < len(parts):
            return parts[i + 1].replace("-", " ")
    return ""


def fetch_workday(slug, search_text=""):
    # Public cxs endpoint. slug is "{host}/{site}"; tenant is the first dotted
    # label of the host. limit maxes out at 20, so paginate by offset.
    # search_text narrows server-side; without it, huge tenants (a national
    # retailer with ~12k store postings, say) drown the relevant roles and hit
    # the 1000-job page cap.
    host, _, site = slug.partition("/")
    tenant = host.split(".")[0]
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    out = []
    offset = 0
    page_size = 20
    total = None  # Workday reports total on the first page only; later pages send 0.
    while True:
        body = {"limit": page_size, "offset": offset, "searchText": search_text,
                "appliedFacets": {}}
        r = SESSION.post(url, json=body, headers=headers, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings", [])
        if total is None:
            total = data.get("total", 0)
        for j in postings:
            ext_path = j.get("externalPath", "")
            loc = (j.get("locationsText") or "").strip()
            # "N Locations" is a summary, not a place; fall back to the path.
            if not loc or re.fullmatch(r"\d+\s+locations?", loc, re.IGNORECASE):
                loc = _workday_primary_location(ext_path)
            out.append({
                "id": ext_path,
                "title": j.get("title", ""),
                "location": loc,
                "url": f"https://{host}/{site}{ext_path}",
                "company": slug,
                "ats": "workday",
                "posted_at": _workday_relative_date(j.get("postedOn", "")),
                "date_source": "workday:postedOn~",  # ~ = approximate
            })
        offset += len(postings)
        # Stop at total, on an empty page, or at a defensive page cap.
        if offset >= total or not postings or len(out) >= 1000:
            break
        time.sleep(SLEEP_BETWEEN_CALLS)
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
}

# iCIMS has no clean public feed. We surface it for manual handling. Workday
# IS scannable when an entry carries a usable slug; a workday entry with no
# slug degrades to manual in run_scan (handled there).
MANUAL_ATS = {"icims", "manual"}


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------

def compile_patterns(terms, boundary=False):
    # boundary=True wraps each term in \b...\b so short tokens like "us" or "PR"
    # match whole words only (fixes "us" matching inside "Sunnyvale").
    pats = []
    for t in terms:
        esc = re.escape(t)
        if boundary:
            esc = r"\b" + esc + r"\b"
        pats.append(re.compile(esc, re.IGNORECASE))
    return pats


def is_remote(title, loc):
    """True when the role reads as remote-US: an explicit remote token in the
    location, a bare 'Remote' in the title, or a whole-country location with no
    specific metro pinned ('United States' alone, not 'City, State, US')."""
    if REMOTE_WORD_RE.search(title or ""):
        return True
    if any(p.search(loc) for p in REMOTE_TOKEN_PATS):
        return True
    if any(p.search(loc) for p in COUNTRY_PATS):
        residual = COUNTRY_RE.sub(" ", loc)
        if not re.search(r"[A-Za-z]", residual):  # nothing left but the country
            return True
    return False


def matches(job, title_pats, location_pats, exclude_pats):
    title = job.get("title", "")
    loc = job.get("location", "")
    if not any(p.search(title) for p in title_pats):
        return False
    if exclude_pats and any(p.search(title) for p in exclude_pats):
        return False
    if location_pats:
        # Location terms match the LOCATION field only; a "Remote" in the title
        # also counts. This stops title words like "distributed" (as in
        # "distributed storage") from faking remote-eligibility. A bare US
        # country marker (United States / USA / U.S.) also qualifies even when
        # it is not in the configured location_terms, so that "City, State, USA"
        # postings reach classify_location() and get quarantined as 'verify'
        # rather than being silently dropped here.
        loc_ok = (any(p.search(loc) for p in location_pats)
                  or any(p.search(loc) for p in COUNTRY_PATS))
        if not (loc_ok or is_remote(title, loc)):
            return False
    # Foreign guard: a role based abroad is not US-eligible even if the title says
    # "remote". Drop when the LOCATION names a foreign country and not the US.
    # US presence uses both US_PATS (adds "us"/"new mexico") and the dotted-safe
    # COUNTRY_PATS, so a multi-region listing like "U.S., Canada" is kept the same
    # way "United States, Canada" is.
    us_present = (any(p.search(loc) for p in US_PATS)
                  or any(p.search(loc) for p in COUNTRY_PATS)
                  or americas_implies_us(loc))
    if any(p.search(loc) for p in FOREIGN_PATS) and not us_present:
        return False
    return True


def classify_location(title, loc, location_pats):
    """Bucket a matched role's location: 'remote', 'verify', or 'onsite/hybrid'.

    'verify' marks a role that matched only via the US country name and also
    pins a specific US state/metro that is not in commuting radius and is not
    remote-tagged. The ATS location field can mislabel a remote role as its
    primary office (e.g. a remote role listed as "San Francisco, CA, USA"), so
    these are kept but quarantined from default views for a human to check the
    JD. A multi-region listing ("United States, Canada") names no US state, so
    it is not quarantined.
    """
    if is_remote(title, loc):
        # A whole-country "remote" can still pin a non-Eastern territory in the
        # title (e.g. "... - Central"). Hold those for a JD check instead of
        # surfacing them as reachable-remote. Body-only locks are caught later,
        # at ingest, by re-reading the fetched JD (see scan_and_ingest).
        if residency_excludes_eastern(title, in_title=True):
            return "verify"
        return "remote"
    if any(p.search(loc) for p in COUNTRY_PATS):
        residual = COUNTRY_RE.sub(" ", loc)
        if _names_us_state(residual) and not any(
                p.search(residual) for p in location_pats):
            return "verify"
    return "onsite/hybrid"


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state(state_path):
    if state_path.exists():
        return set(json.loads(state_path.read_text()))
    return set()


def save_state(seen, state_path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(sorted(seen), indent=2))


def job_key(job):
    return f"{job['ats']}:{job['company']}:{job['id']}"


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def _tag(j):
    """Render the ' [category location_type]' suffix for a match, or '' if empty."""
    t = " ".join(x for x in [j.get("category", ""), j.get("location_type", "")] if x)
    return f" [{t}]" if t else ""


def notify_discord(webhook, new_jobs):
    if not webhook or not new_jobs:
        return
    # Discord caps content length; chunk into batches of ~10 lines.
    lines = [
        f"**{j['title']}** - {j.get('company_display', j['company'])} "
        f"({j['location'] or 'n/a'}){_tag(j)}"
        f"\n{j['url']}"
        for j in new_jobs
    ]
    header = f"**{len(new_jobs)} new role(s)** found {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    chunks, buf = [], header + "\n\n"
    for ln in lines:
        if len(buf) + len(ln) > 1800:
            chunks.append(buf)
            buf = ""
        buf += ln + "\n\n"
    if buf.strip():
        chunks.append(buf)
    for c in chunks:
        try:
            SESSION.post(webhook, json={"content": c}, timeout=REQUEST_TIMEOUT)
            time.sleep(1)
        except requests.RequestException as e:
            print(f"  ! Discord post failed: {e}", file=sys.stderr)


def write_report(new_jobs, manual_companies):
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = HERE / f"new_jobs_{stamp}.md"
    with path.open("w") as f:
        f.write(f"# New job matches - {datetime.now():%Y-%m-%d %H:%M}\n\n")
        if not new_jobs:
            f.write("No new matches this run.\n\n")
        else:
            f.write(f"{len(new_jobs)} new match(es).\n\n")
            for j in new_jobs:
                disp = j.get("company_display", j["company"])
                f.write(f"- **{j['title']}** | {disp} ({j['ats']}) | "
                        f"{j['location'] or 'n/a'}{_tag(j)}\n  {j['url']}\n")
        if manual_companies:
            f.write("\n## Check manually (no public API)\n\n")
            for c in manual_companies:
                f.write(f"- {c['name']} ({c['ats']}): {c.get('careers_url', 'n/a')}\n")
    return path


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run_scan(cfg, seen, verbose=False):
    """Pure scan: fetch, filter, diff. No file writes, no notifications.

    Returns (new_jobs, all_matches, manual). This is the import-safe entry
    point an app or skill calls. `seen` is a set of job keys; the caller owns
    state persistence. Each match dict is enriched with company_display,
    category, and location_type.
    """
    title_pats = compile_patterns(cfg.get("title_terms", []))
    location_pats = compile_patterns(cfg.get("location_terms", []), boundary=True)
    exclude_pats = compile_patterns(cfg.get("exclude_terms", []), boundary=True)

    all_matches = []
    manual = []

    for c in cfg.get("companies", []):
        ats = c["ats"].lower()
        name = c.get("name", c.get("slug", "?"))
        # No usable slug means there is nothing to fetch (e.g. a workday company
        # we could not reach), so it degrades to manual rather than crashing.
        if ats in MANUAL_ATS or not c.get("slug"):
            manual.append({"name": name, "ats": ats,
                           "careers_url": c.get("careers_url", "")})
            continue
        fetcher = FETCHERS.get(ats)
        if not fetcher:
            if verbose:
                print(f"  ? unknown ats '{ats}' for {name}, skipping", file=sys.stderr)
            continue
        try:
            if ats == "workday" and c.get("search_text"):
                jobs = fetcher(c["slug"], search_text=c["search_text"])
            else:
                jobs = fetcher(c["slug"])
        except requests.HTTPError as e:
            if verbose:
                print(f"  ! {name} ({ats}) HTTP {e.response.status_code}, skipping",
                      file=sys.stderr)
            continue
        except requests.RequestException as e:
            if verbose:
                print(f"  ! {name} ({ats}) request failed: {e}, skipping", file=sys.stderr)
            continue
        except Exception as e:
            # A single company's unexpected response shape should never take
            # down the whole scan. Log it and move on.
            if verbose:
                print(f"  ! {name} ({ats}) parse error ({type(e).__name__}: {e}), skipping",
                      file=sys.stderr)
            continue

        hits = [j for j in jobs if matches(j, title_pats, location_pats, exclude_pats)]
        category = c.get("category", "")
        for j in hits:
            j["company_display"] = name
            j["category"] = category
            j["location_type"] = classify_location(
                j.get("title", ""), j.get("location", ""), location_pats)
        if verbose:
            print(f"  {name} ({ats}): {len(jobs)} total, {len(hits)} match filters")
        all_matches.extend(hits)
        time.sleep(SLEEP_BETWEEN_CALLS)

    new_jobs = [j for j in all_matches if job_key(j) not in seen]
    return new_jobs, all_matches, manual


def main():
    ap = argparse.ArgumentParser(
        description="Read-only job discovery across public ATS APIs.")
    ap.add_argument("-c", "--config",
                    default=os.environ.get("JOB_CONFIG") or str(HERE / "companies.yaml"),
                    help="Path to config YAML (default: ./companies.yaml)")
    ap.add_argument("--state", default=None,
                    help="Path to state file (default: per-user data dir)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print matches to terminal; write no state, send no notifications")
    ap.add_argument("--first-run", action="store_true",
                    help="Seed state from current postings, suppress notifications")
    ap.add_argument("--no-discord", action="store_true",
                    help="Skip Discord even if a webhook is configured")
    args = ap.parse_args()

    config_path = Path(args.config).expanduser()
    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    cfg = yaml.safe_load(config_path.read_text())

    state_path = resolve_state_path(args.state)

    # Dry run never reads or writes state: everything is reported as a "match"
    # so you can see exactly what the filters catch.
    seen = set() if args.dry_run else load_state(state_path)

    new_jobs, all_matches, manual = run_scan(cfg, seen, verbose=True)

    if args.dry_run:
        print(f"\n[dry-run] {len(all_matches)} match(es), no state written:\n")
        for j in all_matches:
            disp = j.get("company_display", j["company"])
            print(f"  - {j['title']} | {disp} ({j['ats']}) | {j['location'] or 'n/a'}{_tag(j)}")
            print(f"    {j['url']}")
        if manual:
            print(f"\n[dry-run] {len(manual)} company(ies) need manual checking:")
            for m_ in manual:
                print(f"  - {m_['name']} ({m_['ats']}): {m_.get('careers_url', 'n/a')}")
        return

    # Persist state (everything seen this run).
    for j in all_matches:
        seen.add(job_key(j))
    save_state(seen, state_path)

    report = write_report(new_jobs, manual)
    print(f"\n{len(new_jobs)} new match(es). Report: {report}")
    print(f"State: {state_path}")

    if args.first_run:
        print("First run: state seeded, notifications suppressed.")
        return

    discord_webhook = os.environ.get("DISCORD_WEBHOOK") or cfg.get("discord_webhook", "")
    if not args.no_discord:
        notify_discord(discord_webhook, new_jobs)


if __name__ == "__main__":
    main()
