#!/usr/bin/env python3
"""
freshness.py - posting-age computation and labeling.

Centralizes how the pipeline reasons about how old a posting is, and is honest
about when that age is only an approximation.

Date reliability by ATS:
  lever           : createdAt is a true first-posted timestamp. Reliable.
  ashby           : publishedAt is reliable.
  smartrecruiters : releasedDate is reliable.
  greenhouse      : the board listing only has updated_at, which changes on any
                    edit, so it is an UPPER BOUND on freshness, not a posting
                    date. upgrade_greenhouse_date() fetches the per-job
                    first_published to replace it when possible.
"""

from datetime import datetime, timezone

import requests

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "personal-job-monitor/1.0"})


def parse_iso(s):
    if not s:
        return None
    try:
        # Handle trailing Z and missing tz.
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def age_hours(posted_at, now=None):
    """Hours since posted_at, or None if undatable."""
    dt = parse_iso(posted_at)
    if not dt:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 3600.0


def is_approximate(date_source):
    return bool(date_source) and date_source.endswith("~")


def freshness_label(posted_at, date_source):
    """Human string like 'posted 19h ago (lever)' or 'age unknown'."""
    h = age_hours(posted_at)
    if h is None:
        return "age unknown"
    approx = is_approximate(date_source)
    verb = "updated" if approx else "posted"
    src = (date_source or "").rstrip("~")
    if h < 48:
        when = f"{int(round(h))}h ago"
    else:
        when = f"{int(round(h / 24))}d ago"
    tail = ", approx" if approx else ""
    return f"{verb} {when} ({src}{tail})"


def passes_max_age(posted_at, date_source, max_age_hours, keep_unknown=True):
    """Decision for the freshness filter.

    keep_unknown=True implements the chosen policy: an undatable posting is
    kept (and flagged elsewhere), not silently dropped.
    """
    h = age_hours(posted_at)
    if h is None:
        return keep_unknown
    return h <= max_age_hours


def upgrade_greenhouse_date(company, ext_id):
    """Fetch a Greenhouse job's true first_published.

    The board listing only gives updated_at. The per-job endpoint includes
    first_published, the real posting date. Returns (iso_or_empty, source).
    Called only for candidate roles (post title-filter), so volume is low.

    The URL comes from job_generate.posting_endpoint, the single definition,
    so this cannot drift from the fetchers and the liveness check.
    """
    # Lazy import: a module-level import here would close a cycle that is
    # currently held open only by another function-local import (job_generate
    # -> gate -> fit -> freshness, with gate.py:533's import of fit being
    # function-local).
    import job_generate

    url = job_generate.posting_endpoint(
        {"ats": "greenhouse", "company": company, "ext_id": ext_id})
    try:
        data = SESSION.get(url, timeout=20).json()
    except (requests.RequestException, ValueError):
        return "", "greenhouse:updated_at~"
    fp = data.get("first_published") or ""
    if fp:
        return fp, "greenhouse:first_published"
    up = data.get("updated_at") or ""
    return up, "greenhouse:updated_at~"
