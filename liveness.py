#!/usr/bin/env python3
"""
liveness.py - is this posting still up?

334 leads sit in `discovered`, accumulating since June, and an unknown but
large share of them are expired postings that nothing marks. The gate finds
out the expensive way: three gate runs on 2026-07-28 each spent an LLM call
to discover the posting was gone (zetaglobal's Greenhouse role 404s while the
row still holds 11,755 characters of stored JD, so it looks alive in the
database). This asks the same question for free, with one unauthenticated GET
and no model call, so the dead weight can be cleared before any API spend.

Fails safe, in the opposite direction from the gate. Marking a LIVE posting
closed silently removes a real opportunity from the pipeline, and nothing
downstream would ever surface it again. So every uncertain case is `unknown`
and `unknown` is never acted on: no endpoint, a network error, a timeout, a
5xx, a 429, an unparseable body, a body whose shape we do not recognize. Only
an unambiguous "this posting is gone" answer returns `closed`.

The endpoint comes from job_generate.posting_endpoint, the same helper the JD
fetchers use. There is deliberately no second copy of those URLs here.

Politeness is the caller's job (this issues one request per call). Sweep
callers must sleep job_monitor.SLEEP_BETWEEN_CALLS between rows.

check()'s catch-all around the payload classifier keeps a malformed body from
aborting a sweep, but it must not also hide a genuine bug in the classifier
itself. `on_classifier_error` (see check()) fires only when the classifier
raises for a shape it does not already have a name for; known malformed
shapes (an ashby board entry that is not an object) are handled explicitly
and never trip it.
"""

import job_generate
import job_monitor

OPEN = "open"
CLOSED = "closed"
UNKNOWN = "unknown"

# HTTP codes that mean the posting itself is gone. Nothing else does: a 403 is
# a blocked client, a 401 is an auth-gated board, a 5xx is the ATS having a bad
# day, and every one of those describes our request rather than the posting.
GONE_STATUSES = (404, 410)


def _get(url):
    """Default fetcher. job_monitor.SESSION carries the polite User-Agent."""
    return job_monitor.SESSION.get(url, timeout=job_monitor.REQUEST_TIMEOUT)


def check(row, fetch=None, on_classifier_error=None):
    """Classify one job row as 'open', 'closed', or 'unknown'.

    `fetch` is a callable(url) returning a requests-style response, injectable
    for tests. Any exception it raises (timeout, connection error, anything)
    is an `unknown`, never a `closed`.

    `on_classifier_error`, if given, is called with no arguments when
    `_payload_verdict` raises for a reason it does not already know how to
    name (a real bug in the classifier, not a payload shape it recognizes and
    handles). The verdict below is still `unknown` either way; this only lets
    a caller notice that something unanticipated happened instead of the
    failure being silent in a clean-looking run. See cmd_prune for the caller
    that uses it.
    """
    url = job_generate.posting_endpoint(row)
    if not url:
        return UNKNOWN  # unsupported ATS: no public endpoint to ask
    fetch = fetch or _get
    try:
        resp = fetch(url)
        status = resp.status_code
    except Exception:
        return UNKNOWN
    if status in GONE_STATUSES:
        return CLOSED
    if status != 200:
        return UNKNOWN
    try:
        data = resp.json()
    except Exception:
        return UNKNOWN
    try:
        return _payload_verdict(row["ats"], row["ext_id"], data)
    except Exception:
        # An unexpected payload shape is uninterpretable, not a `closed`, and
        # it must not abort a sweep of hundreds of rows on one bad row. But it
        # is also not supposed to happen: every payload shape we already know
        # about is handled inside _payload_verdict without raising (see the
        # ashby non-dict-entry guard below). Reaching this except is a signal
        # that the classifier hit something it does not have a name for, so
        # tell the caller.
        if on_classifier_error is not None:
            on_classifier_error()
        return UNKNOWN


def _payload_verdict(ats, ext, data):
    """Classify a 200 by whether its payload still contains the posting.

    One ATS can answer this. Ashby's endpoint is the whole board list, so a
    pulled posting is simply absent from it. That conclusion only holds when
    the board is healthy AND populated: an empty or missing `jobs` list is a
    board that is paused, depublished, or served as an error envelope, and
    reading it as "gone" would mark every ashby row for that company closed in
    a single sweep.

    Greenhouse cannot. Every dead greenhouse posting observed so far answers
    404, and a 200 carrying an empty or non-string `content` has never been
    observed at all, so treating that shape as `closed` would buy nothing real
    while risking a false `closed` on a shape we do not understand. It
    resolves to unknown.

    Everywhere else a 200 that parsed as a non-empty object is taken as open,
    which is the safe direction: an `open` is never acted on.
    """
    if not isinstance(data, dict):
        return UNKNOWN
    if ats == "ashby":
        jobs = data.get("jobs")
        if not isinstance(jobs, list) or not jobs:
            return UNKNOWN  # no board, or an empty one; we cannot tell
        if not all(isinstance(j, dict) for j in jobs):
            # A board entry that is not itself an object. This is a known,
            # anticipated malformed-payload shape (not a code bug), so it is
            # named here rather than left to raise and reach check()'s
            # catch-all, which exists for the unnamed case instead.
            return UNKNOWN
        return OPEN if any(str(j.get("id")) == str(ext) for j in jobs) else CLOSED
    if ats == "greenhouse":
        content = data.get("content")
        return OPEN if isinstance(content, str) and content.strip() else UNKNOWN
    return OPEN if data else UNKNOWN
