"""openjobs.py - wide-net lead discovery over the open-jobs public corpus.

The second discovery source, running beside job_monitor.run_scan rather than
replacing it. The ATS scanner is the deep watch on the 191 scannable boards in
companies.yaml; this is the wide net over ~2M postings from ~65,000 boards,
including ATS families (iCIMS, Oracle Cloud, Dayforce, Workable, Taleo) that
publish no feed we could scan ourselves.

Three public endpoints, documented by the project as fixed contracts:

    POST /embed              text in, 1536-dim vector out
    GET  /data/manifest.json the group tree
    GET  /data/centroids.bin one centroid per tree node, float16
    GET  /data/groups/<id>.json  one group's postings, with full JD and vectors

Cost. `bin/daily.sh` is deliberately deterministic and free, and this stage
keeps it that way. The embed call is the ONLY request that carries our data,
and it fires only when ideal-jd.md changes: its vector is cached against the
sha256 of the text that produced it. Ranking is local. Group downloads are
anonymous static files. A daily run with an unchanged JD costs no API spend at
all.

Failure. Discovery fails SAFE, the mirror image of the Fit Gate, and for the
same reason the liveness sweep does: a lead missed today comes back tomorrow,
a broken run takes the digest with it. Every error path here (no JD, a 429, a
network error, a corrupt group, an unparseable manifest) yields zero
candidates and a logged reason. Nothing in this module raises into daily.sh.

This module is import-safe and side-effect-free apart from its own on-disk
cache: it returns candidate dicts and never writes to the database. job_cli's
openjobs_and_ingest does the dedup and the writing, so a second caller can be
added without duplicating either. There is no MCP tool for this stage yet; the
seam exists, the second consumer does not.
"""
import base64
import calendar
import hashlib
import re
import json
import os
import time
from pathlib import Path

import numpy as np
import requests

import job_monitor as jm

HERE = Path(__file__).resolve().parent

BASE_URL = os.environ.get("JOB_OPENJOBS_URL", "https://backend.dehnbostele.workers.dev")
IDEAL_JD_PATH = Path(os.environ.get("JOB_IDEAL_JD", HERE / "ideal-jd.md"))

# How many nearest groups to pull. Each is roughly 400 postings / 2.5MB, so 12
# is ~3,000 postings and ~30MB. Bigger buys recall we cannot triage; the cap on
# ingested leads is the real control.
DEFAULT_GROUPS = 12
# Hard cap on leads ingested per run. `discovered` already holds hundreds of
# rows, so the binding constraint is triage capacity, not recall.
DEFAULT_TOP = 15

REQUEST_TIMEOUT = 60
MANIFEST_MAX_AGE = 6 * 3600  # seconds; matches the corpus's own rebuild cadence

SOURCE = "openjobs"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": jm.SESSION.headers["User-Agent"]})


class CorpusError(Exception):
    """Any failure reaching or parsing the corpus. Always caught before it
    escapes discover(); it exists so the failure paths are explicit."""


# -- cache ------------------------------------------------------------------

def cache_dir():
    """Where the manifest, centroids, group files and ideal vector live.

    Beside jobs.db on the host, so the whole of job-hound's mutable state sits
    in one directory. JOB_OPENJOBS_CACHE overrides; the test suite sets it.
    """
    override = os.environ.get("JOB_OPENJOBS_CACHE")
    if override:
        return Path(override)
    db = os.environ.get("JOB_DB")
    root = Path(db).resolve().parent if db else HERE
    return root / "openjobs-cache"


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


STATUS_FILE = "last-run.json"


def write_status(summary, cache=None):
    """Record what the last wide-net run did, for the digest to report.

    Discovery fails SAFE, which is right, but on 2026-09-01 the corpus `/embed`
    endpoint was down and the only trace was a line in daily.log that nobody
    reads: a run that found 15 leads and a run where the corpus was unreachable
    left the same observable state. Fail-safe was correct; fail-silent was not.

    Best effort by design. Never raises, because housekeeping must not be able
    to cost the run its leads.
    """
    try:
        cache = Path(cache) if cache else cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        (cache / STATUS_FILE).write_text(json.dumps(
            {**summary, "at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())}))
    except Exception:
        pass


# A status older than this is not describing today's run. Under a day, so
# yesterday's success can never stand in for a run that timed out or was
# switched off, and comfortably over the gap between daily crons.
STATUS_MAX_AGE = 20 * 3600


def read_status(cache=None, max_age=STATUS_MAX_AGE):
    """The last run's summary, or None when there is no CURRENT one.

    Age matters. `bin/daily.sh` caps the stage with `timeout` and
    OPENJOBS_ENABLED=0 switches it off entirely; in both cases write_status
    never runs, and an unbounded read would keep reporting the last success in
    every future digest. An unreadable or undatable record is treated as stale
    for the same reason: fail toward silence, never toward a stale claim.
    """
    try:
        cache = Path(cache) if cache else cache_dir()
        status = json.loads((cache / STATUS_FILE).read_text())
        stamped = calendar.timegm(time.strptime(status["at"][:19],
                                                "%Y-%m-%dT%H:%M:%S"))
        if time.time() - stamped > max_age:
            return None
        return status
    except Exception:
        return None


# -- network boundaries (injected in tests) ---------------------------------

def http_get(path, binary=False):
    try:
        r = SESSION.get(f"{BASE_URL}{path}", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.content if binary else r.json()
    except Exception as e:
        raise CorpusError(f"GET {path} failed: {type(e).__name__}: {e}") from e


def http_embed(text):
    """POST the ideal JD and get its vector. The only call that sends our data.

    Rate-limited by the corpus to 10 per 10 minutes per IP. We are nowhere near
    it because this fires only when ideal-jd.md changes.
    """
    try:
        r = SESSION.post(f"{BASE_URL}/embed", json={"text": text},
                         timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()["vector"]
    except Exception as e:
        raise CorpusError(f"embed failed: {type(e).__name__}: {e}") from e


# -- the ideal-JD vector ----------------------------------------------------

def ideal_vector(text, cache=None, embed=None, recipe="", dims=0):
    """Return the embedding of `text`, embedding only when something changed.

    The cache key is (sha256 of the text, corpus recipe, dims), not the text
    alone, and a mismatch on ANY of the three is a cache MISS that re-embeds.

    Both halves of that matter and neither is theoretical. Keying on the text
    alone meant that when upstream swapped the embedding model but kept 1536
    dims, a stale vector got ranked against the new space: not an empty run, a
    run of confidently ranked semantic noise, with nothing logged. And treating
    a mismatch as a reason to BAIL rather than re-embed meant the stage went
    dark permanently the first time dims changed, since nothing ever replaced
    the cached vector. Silence looks exactly like a quiet day.

    Re-embedding costs one call against a limit of 10 per 10 minutes.

    The cache also records the text itself. The 2026-08-31 hand-run of the
    upstream toolchain left two ideal-JD versions on disk with different salary
    targets and no record of which produced the shortlist. A vector whose
    provenance is not written down is a result nobody can reproduce.
    """
    cache = Path(cache) if cache else cache_dir()
    embed = embed or http_embed
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / "ideal.json"
    digest = text_hash(text)
    if path.exists():
        try:
            saved = json.loads(path.read_text())
            if (saved.get("sha256") == digest
                    and saved.get("recipe", "") == recipe
                    and saved.get("dims", 0) == dims):
                return saved["vector"]
        except (ValueError, KeyError, OSError):
            pass  # unreadable cache is a miss, not a failure
    vector = embed(text)
    path.write_text(json.dumps({
        "vector": vector,
        "sha256": digest,
        "recipe": recipe,
        "dims": dims,
        "source_text": text,
        "embedded_at": int(time.time() * 1000),
    }))
    return vector


# -- the group tree ---------------------------------------------------------

def load_centroids(blob, dims):
    """The centroid matrix: float16 on the wire, one row per tree-node id."""
    return np.frombuffer(blob, dtype=np.float16).astype(np.float32).reshape(-1, dims)


def load_manifest(get=None, cache=None, max_age=MANIFEST_MAX_AGE):
    """Return (manifest, centroids), re-downloading when the copy is stale."""
    get = get or http_get
    cache = Path(cache) if cache else cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    mpath, cpath = cache / "manifest.json", cache / "centroids.bin"
    fresh = (mpath.exists() and cpath.exists()
             and time.time() - mpath.stat().st_mtime < max_age)
    if not fresh:
        # Fetch BOTH, then publish BOTH. Writing the manifest first meant a
        # failure between the two calls left a new manifest, with a fresh
        # mtime, beside the old centroids. The freshness check only looks at
        # the manifest, so the next run paired them and indexed off the end of
        # the centroid matrix.
        manifest_blob = json.dumps(get("/data/manifest.json"))
        centroid_blob = get("/data/centroids.bin", binary=True)
        tmp_m, tmp_c = mpath.with_suffix(".tmp"), cpath.with_suffix(".tmp")
        tmp_m.write_text(manifest_blob)
        tmp_c.write_bytes(centroid_blob)
        # Centroids first. The freshness check reads the MANIFEST's mtime, so
        # publishing it last means a kill between these two calls leaves the
        # pair stale-but-consistent rather than fresh-but-mismatched.
        os.replace(tmp_c, cpath)
        os.replace(tmp_m, mpath)
    try:
        m = json.loads(mpath.read_text())
    except (ValueError, OSError) as e:
        raise CorpusError(f"unparseable manifest: {e}") from e
    # Validate the SHAPE rather than trusting the exception type to tell a bad
    # payload from a bug in this module. A public worker serving a JSON string
    # where an object belongs raised TypeError/AttributeError, which is exactly
    # what a real bug here raises, so no handler could distinguish them.
    if not isinstance(m, dict) or not isinstance(m.get("tree"), list):
        raise CorpusError("manifest is not an object with a tree")
    if not isinstance(m.get("dims"), int) or m["dims"] <= 0:
        raise CorpusError(f"manifest has no usable dims: {m.get('dims')!r}")
    if not all(isinstance(n, dict) and "id" in n and "children" in n
               for n in m["tree"]):
        raise CorpusError("manifest tree holds something that is not a node")
    try:
        return m, load_centroids(cpath.read_bytes(), m["dims"])
    except (ValueError, OSError) as e:
        raise CorpusError(f"unusable centroids: {e}") from e


def rank_groups(manifest, centroids, vector, k):
    """Rank every LEAF by cosine between its centroid and the ideal-JD vector.

    Internal nodes are skipped: their centroids are averages of their children
    and downloading one would mean downloading the whole subtree.
    """
    v = np.asarray(vector, dtype="float32")
    v = v / (np.linalg.norm(v) + 1e-9)
    sims = centroids @ v
    leaves = [n for n in manifest["tree"] if not n["children"]]
    leaves.sort(key=lambda n: -sims[n["id"]])
    return [(n["id"], float(sims[n["id"]])) for n in leaves[:k]]


def group_jobs(leaf_id, built_at, get=None, cache=None):
    """One group's postings, cached under the build that produced it.

    Namespacing by built_at matters: the corpus is rebuilt daily and group ids
    are NOT stable between builds, so a cache keyed on id alone serves
    yesterday's postings under today's numbering.
    """
    get = get or http_get
    cache = Path(cache) if cache else cache_dir()
    gdir = cache / "groups" / str(built_at)
    gdir.mkdir(parents=True, exist_ok=True)
    path = gdir / f"{leaf_id}.json"
    if not path.exists():
        path.write_text(json.dumps(get(f"/data/groups/{leaf_id}.json")))
    try:
        g = json.loads(path.read_text())
    except (ValueError, OSError) as e:
        raise CorpusError(f"unparseable group {leaf_id}: {e}") from e
    if not isinstance(g, dict) or not isinstance(g.get("jobs", []), list):
        raise CorpusError(f"group {leaf_id} is not an object with a jobs list")
    return g.get("jobs", [])


def prune_cache(cache=None, keep=2):
    """Drop group files from builds we no longer fetch. Best-effort."""
    cache = Path(cache) if cache else cache_dir()
    root = cache / "groups"
    if not root.is_dir():
        return 0
    # Sort numerically. built_at is epoch ms, so in production every directory
    # name is 13 digits and a string sort happens to agree; it stops agreeing
    # the moment one does not, and then the newest build is the one deleted.
    def newest_first(d):
        return (1, int(d.name)) if d.name.isdigit() else (0, 0)

    builds = sorted((d for d in root.iterdir() if d.is_dir()),
                    key=newest_first, reverse=True)
    dropped = 0
    for stale in builds[keep:]:
        for f in stale.iterdir():
            try:
                f.unlink()
                dropped += 1
            except OSError:
                pass
        try:
            stale.rmdir()
        except OSError:
            pass
    return dropped


HEX_ID_RE = re.compile(r"[0-9a-fA-F-]{20,}\Z")


def _slugify(name):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def display_name(board_slug, published_name):
    """A company name for a human to read. Cosmetic, and ONLY cosmetic.

    This is deliberately separate from `company`, which is stored exactly as
    the board publishes it. An earlier version prettified `company` itself and
    broke two things at once: it rewrote `example-co.com`, a real Ashby board
    slug, into a 404 that turned the gate's fetch into an ERROR; and it
    collapsed `careers.acme.com` and `careers.beta.com` onto one slug, so two
    employers could share a uid and one lead vanished as a false duplicate.

    Nothing here can do that any more, because nothing here feeds a key or a
    URL. The worst a bad guess costs is an ugly name on a card.
    """
    board_slug = (board_slug or "").strip()
    published = (published_name or "").strip()
    if published and published.lower() != board_slug.lower():
        return published
    if "." in board_slug and not HEX_ID_RE.fullmatch(board_slug):
        first = board_slug.split(".")[0]
        if first:
            return first
    return board_slug


def score_postings(postings, vector, fallback=0.0):
    """Score each posting against the ideal-JD vector. Returns [(raw, sim)].

    The group centroid decides WHICH groups are worth downloading; it must not
    also decide the order inside them. Every posting carries its own float32
    vector, and using the centroid for both gave all 15 ingested leads the same
    score, which quietly turned the top-N cap into "the first N rows of the
    nearest group file".

    A posting whose vector is missing or unparseable keeps the group score
    rather than being dropped: that costs it precision, not its place.
    """
    v = np.asarray(vector, dtype="float32")
    v = v / (np.linalg.norm(v) + 1e-9)
    out = []
    for raw in postings:
        try:
            vec = np.frombuffer(base64.b64decode(raw["v"]), dtype="float32")
            sim = float(vec @ v) if vec.shape == v.shape else fallback
        except (TypeError, ValueError, KeyError, IndexError):
            sim = fallback
        out.append((raw, sim))
    out.sort(key=lambda pair: -pair[1])
    return out


# -- candidate mapping ------------------------------------------------------

def to_job(raw, sim, location_pats):
    """Map one corpus posting into the dict shape job_monitor produces.

    `company` is the BOARD SLUG, not the display name, because job-hound's uid
    is ats:company:ext_id and the scanner puts the slug there. Matching that
    exactly is what makes a posting both sources see dedup for free.
    """
    title = raw.get("title") or ""
    loc = raw.get("location") or ""
    jd = raw.get("jd") or ""
    posted = ""
    seen = raw.get("seen") or 0
    if seen:
        posted = time.strftime("%Y-%m-%d", time.gmtime(seen / 1000))
    return {
        "id": str(raw["id"]),
        "title": title,
        "location": loc,
        "url": raw.get("url") or "",
        # Stored exactly as published: this is the uid's middle field and, for
        # four ATSes, a path segment in job_generate.posting_endpoint.
        "company": raw["slug"],
        "company_display": display_name(raw["slug"], raw.get("company")),
        "ats": raw["ats"],
        # `seen` is when the crawler first saw the posting, not when it was
        # posted. The trailing ~ is the codebase's honest-provenance marker;
        # freshness.py reads it and labels the age approximate.
        "posted_at": posted,
        "date_source": "openjobs:first_seen~",
        "description": jd,
        "location_type": jm.classify_location(title, loc, location_pats),
        "source": SOURCE,
        "sim": sim,
    }


def discover(cfg, jd_text=None, groups=DEFAULT_GROUPS, cache=None,
             get=None, embed=None, verbose=False, problems=None):
    """Return candidate job dicts, best match first.

    Fails safe: every corpus and network failure yields an empty list and a
    logged reason rather than an exception, so the daily digest still goes out.
    A bug in this module is NOT treated that way and will propagate.

    `problems` is an optional list that FATAL reasons are appended to. Failing
    safe swallows the exception on purpose, but the caller still has to be able
    to tell "the corpus is down" from "there was nothing new", and returning a
    bare [] for both is what made the first version of the digest health line
    report a 503 as a healthy quiet day. Per-group skips are logged and not
    appended: one bad group is not a failed run.

    No database knowledge and no writes: dedup and the per-run cap belong to
    job_cli.openjobs_and_ingest, which is the only thing that knows what is
    already in the pipeline.
    """
    def log(msg):
        if verbose:
            print(f"  {msg}")

    def fatal(msg):
        """A reason the run produced nothing. Logged AND handed to the caller."""
        log(f"! {msg}")
        if problems is not None:
            problems.append(msg)
        return []

    if jd_text is None:
        try:
            jd_text = IDEAL_JD_PATH.read_text()
        except OSError as e:
            return fatal(f"no ideal JD at {IDEAL_JD_PATH} ({e})")
    if not jd_text.strip():
        return fatal("ideal JD is empty")

    # Manifest FIRST: the ideal vector is cached against the corpus's recipe and
    # dims, so we have to know them before we can decide the cache is valid.
    try:
        manifest, centroids = load_manifest(get=get, cache=cache)
        vector = ideal_vector(jd_text, cache=cache, embed=embed,
                              recipe=manifest.get("recipe", ""),
                              dims=manifest.get("dims", 0))
        ranked = rank_groups(manifest, centroids, vector, groups)
    except CorpusError as e:
        return fatal(str(e))
    except (OSError, ValueError, KeyError, IndexError, MemoryError) as e:
        # Deliberately NOT a bare `except Exception`. A TypeError or an
        # AttributeError here is a bug in this module, and swallowing it as
        # "the corpus is down" is how a dead stage passes for a quiet day.
        return fatal(f"corpus data unusable ({type(e).__name__}: {e})")

    title_pats = jm.compile_patterns(cfg.get("title_terms", []))
    location_pats = jm.compile_patterns(cfg.get("location_terms", []), boundary=True)
    exclude_pats = jm.compile_patterns(cfg.get("exclude_terms", []), boundary=True)

    built_at = manifest.get("built_at", 0)
    out = []
    for leaf_id, leaf_sim in ranked:
        try:
            postings = group_jobs(leaf_id, built_at, get=get, cache=cache)
        except CorpusError as e:
            # One bad group must never take down the run, exactly as one
            # company's bad data must never take down run_scan.
            log(f"! group {leaf_id}: {e}, skipping")
            continue
        for raw, sim in score_postings(postings, vector, fallback=leaf_sim):
            if not isinstance(raw, dict) or not raw.get("slug") or not raw.get("ats"):
                log(f"! posting in group {leaf_id} is not a usable record, skipping")
                continue
            try:
                job = to_job(raw, sim, location_pats)
            except KeyError as e:
                log(f"! posting in group {leaf_id} missing {e}, skipping")
                continue
            # Embedding similarity ranks well and filters badly. The configured
            # term lists are the cheap guard against a semantically-near role
            # that is plainly wrong.
            if not jm.matches(job, title_pats, location_pats, exclude_pats):
                continue
            # The body-residency check scan_and_ingest pays extra HTTP fetches
            # for is free here: the JD is already in hand.
            if (job["location_type"] == "remote"
                    and jm.residency_excludes_eastern(job["description"])):
                job["location_type"] = "verify"
            out.append(job)

    out.sort(key=lambda j: -j["sim"])
    # Housekeeping last, and never at the cost of the run. Each build's group
    # files are ~37MB and the corpus rebuilds daily, so an unpruned cache grows
    # about 13GB a year onto a host that has 8GB free.
    try:
        prune_cache(cache=cache)
    except Exception as e:
        log(f"! cache prune failed ({type(e).__name__}: {e}); continuing")
    log(f"{len(out)} candidates from {groups} groups "
        f"(corpus of {manifest.get('jobs', 0):,} postings)")
    return out
