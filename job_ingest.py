"""Drain web-inbox job submissions: fetch JD, LLM fit verdict,
auto-draft packages when llm_fit_score >= 90, and ping Discord.

Reads the spool at $JOB_INBOX_DIR/pending, writes $JOB_INBOX_DIR/processed,
and is the ONLY writer of jobs.db in this flow.
"""
from urllib.parse import urlparse, parse_qs
import json
import os
import re
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import yaml
import job_monitor
import job_fetch
from job_generate import _strip_html
import fit
import gate
import job_generate
import notify
from jobdb import JobDB, DBPathError, make_job_uid, resolve_db_path

# Provenance stamped on every lead this path ingests. Web-inbox
# submissions are not scanner discoveries and must never be counted as such:
# an inbound lead answered by a recruiter flatters the response rate if it is
# filed as one the operator went out and found.
SOURCE = "mission-control"


def _match_greenhouse_board(host, path, parts, query):
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io") \
            and len(parts) >= 3 and parts[1] == "jobs":
        return {"ats": "greenhouse", "company": parts[0], "ext_id": parts[2]}
    return None


def _match_greenhouse_embed(host, path, parts, query):
    if (host == "greenhouse.io" or host.endswith(".greenhouse.io")) \
            and path.rstrip("/").endswith("embed/job_app"):
        if query.get("for") and query.get("token"):
            return {"ats": "greenhouse", "company": query["for"][0],
                    "ext_id": query["token"][0]}
    return None


def _match_lever(host, path, parts, query):
    if host == "jobs.lever.co" and len(parts) >= 2:
        return {"ats": "lever", "company": parts[0], "ext_id": parts[1]}
    return None


def _match_ashby(host, path, parts, query):
    if host == "jobs.ashbyhq.com" and len(parts) >= 2:
        return {"ats": "ashby", "company": parts[0], "ext_id": parts[1]}
    return None


def _match_smartrecruiters(host, path, parts, query):
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") \
            and len(parts) >= 2:
        # The posting segment is "{postingId}-{title-slug}"; the id is the leading
        # alphanumeric run before the first hyphen.
        m = re.match(r"^([A-Za-z0-9]+)", parts[1])
        ext = m.group(1) if m else parts[1]
        return {"ats": "smartrecruiters", "company": parts[0], "ext_id": ext}
    return None


# Workday serves the same posting with and without a locale prefix
# ("/en-US/careers/job/..." and "/careers/job/..." both return 200), but the cxs
# endpoint takes neither, so the segment comes off before the site slug is read.
_WORKDAY_LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")


def _match_workday(host, path, parts, query):
    """Parse a Workday external posting URL into the shape the scanner stores.

    job_monitor.fetch_workday records company="{host}/{site}" and
    ext_id=externalPath ("/job/<Location>/<Title>_R-123"), and
    job_generate.posting_endpoint rebuilds the cxs URL from exactly that pair.
    Producing anything else here would both miss the uid dedup against a
    scanner hit and build a cxs URL that 404s, which the gate reports as ERROR
    and treats like DO_NOT_APPLY.

    Only the vendor host is matched. A Workday tenant behind a vanity domain
    has no way to tell us its site slug, so it still falls through.
    """
    if host != "myworkdayjobs.com" and not host.endswith(".myworkdayjobs.com"):
        return None

    def read(segs):
        # site, then "job", then the posting path itself, which is always at
        # least a location and a title segment ("/job/<Location>/<Title>_R-1").
        if len(segs) < 4 or segs[1] != "job":
            return None
        return {"ats": "workday",
                "company": f"{host}/{segs[0]}",
                "ext_id": "/" + "/".join(segs[1:])}

    # Read the path as-is BEFORE treating the first segment as a locale, so a
    # site slug that merely looks like one ("/us/job/...") is not eaten.
    direct = read(parts)
    if direct:
        return direct
    if parts and _WORKDAY_LOCALE_RE.match(parts[0]):
        return read(parts[1:])
    return None


# Matchers are tried in order; add a new ATS by appending a matcher function.
_MATCHERS = [_match_greenhouse_board, _match_greenhouse_embed, _match_lever,
             _match_ashby, _match_smartrecruiters, _match_workday]


def parse_posting_url(url: str):
    """Return {ats, company, ext_id} for a supported posting URL, else None.

    Supports Greenhouse (hosted board + embed), Lever, Ashby, SmartRecruiters,
    and Workday.
    Extend by adding a matcher function to _MATCHERS.
    """
    try:
        u = urlparse(url)
    except (ValueError, AttributeError):
        return None
    host = (u.hostname or "").lower()
    path = u.path
    parts = [p for p in u.path.split("/") if p]
    query = parse_qs(u.query)
    for matcher in _MATCHERS:
        result = matcher(host, path, parts, query)
        if result:
            return result
    return None


def fetch_posting_meta(parsed):
    """Fetch {title, location, description} for a parsed posting.

    The endpoint comes from job_generate.posting_endpoint, the single
    definition, so this unattended path and the JD fetchers cannot drift apart
    about which URL a posting lives at. Only the parsing below is per-ATS.
    """
    ats, ext = parsed["ats"], parsed["ext_id"]
    url = job_generate.posting_endpoint(parsed)
    if ats == "greenhouse":
        data = job_monitor.SESSION.get(url, timeout=20).json()
        return {
            "title": data.get("title", ""),
            "location": (data.get("location") or {}).get("name", ""),
            "description": _strip_html(data.get("content", "")),
        }
    if ats == "lever":
        data = job_monitor.SESSION.get(url, timeout=20).json()
        parts = [data.get("descriptionPlain", "")]
        for lst in data.get("lists", []):
            # `text` is the section heading, `content` the bullets under it.
            # See the same fix in job_generate.fetch_description; these two
            # must stay identical or the ingest and gate paths disagree.
            head = _strip_html(lst.get("text", ""))
            body = _strip_html(lst.get("content", ""))
            parts.append(f"{head}\n{body}".strip() if head else body)
        return {
            "title": data.get("text", ""),
            "location": (data.get("categories") or {}).get("location", ""),
            "description": "\n\n".join(p for p in parts if p),
        }
    if ats == "ashby":
        # Ashby exposes a per-board listing; find the job by id.
        data = job_monitor.SESSION.get(url, timeout=20).json()
        for j in data.get("jobs", []):
            if str(j.get("id")) == str(ext):
                desc = j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml", ""))
                return {
                    "title": j.get("title", ""),
                    "location": j.get("location", "") or j.get("locationName", ""),
                    "description": desc,
                }
        return {"title": "", "location": "", "description": ""}
    if ats == "smartrecruiters":
        data = job_monitor.SESSION.get(url, timeout=20).json()
        loc = data.get("location") or {}
        loc_str = ", ".join(p for p in [loc.get("city"), loc.get("region"),
                                        loc.get("country")] if p)
        if loc.get("remote"):
            loc_str = (loc_str + " (remote)").strip()
        sections = (data.get("jobAd") or {}).get("sections", {}) or {}
        chunks = [_strip_html((sections.get(k) or {}).get("text", ""))
                  for k in ("jobDescription", "qualifications", "additionalInformation")]
        return {
            "title": data.get("name", ""),
            "location": loc_str,
            "description": "\n\n".join(c for c in chunks if c),
        }
    if ats == "workday":
        # Same payload job_generate.fetch_description reads; the location here
        # is the field the posting actually publishes ("Remote - United
        # States"), which is what the gate's location overlay needs.
        data = job_monitor.SESSION.get(url, timeout=20).json()
        info = data.get("jobPostingInfo") or {}
        # postedOn is relative text ("Posted Yesterday"), so the date is
        # approximate and the source string carries the ~ that says so. Same
        # helper and same label job_monitor.fetch_workday uses for a scanner
        # hit, so one posting cannot end up with two date provenances.
        posted_at = job_monitor._workday_relative_date(info.get("postedOn", ""))
        return {
            "title": info.get("title", ""),
            "location": info.get("location", ""),
            "description": _strip_html(info.get("jobDescription", "")),
            "posted_at": posted_at,
            "date_source": "workday:postedOn~" if posted_at else "",
        }
    raise ValueError(f"no metadata fetcher for ats '{ats}'")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _inbox_dirs(base):
    base = Path(base).expanduser()
    pending = base / "pending"
    processed = base / "processed"
    pending.mkdir(parents=True, exist_ok=True)
    processed.mkdir(parents=True, exist_ok=True)
    return pending, processed


def read_pending(base):
    pending, _ = _inbox_dirs(base)
    out = []
    for f in sorted(pending.glob("*.json")):
        try:
            out.append((f, json.loads(f.read_text())))
        except (json.JSONDecodeError, OSError):
            # Surface unreadable files as (file, None) so main() can record an
            # error and clear them, instead of leaving them in pending forever.
            out.append((f, None))
    return out


def write_processed(base, record):
    _, processed = _inbox_dirs(base)
    safe_id = Path(str(record["id"])).name
    dest = processed / f"{safe_id}.json"
    tmp = processed / f".{safe_id}.tmp"
    tmp.write_text(json.dumps(record, indent=2))
    tmp.replace(dest)
    return dest


def remove_pending(pending_file):
    try:
        Path(pending_file).unlink()
    except OSError:
        pass


def _votes_dirs(base):
    root = Path(base).expanduser() / "votes"
    processed = root / "processed"
    failed = root / "failed"
    for d in (root, processed, failed):
        d.mkdir(parents=True, exist_ok=True)
    return root, processed, failed


def _move_spool_file(f, dest_dir):
    """Move a vote file out of the spool. An OSError here must not kill the
    drain; the file stays behind and the next timer tick retries it."""
    try:
        f.replace(dest_dir / f.name)
    except OSError as e:
        print(f"job_ingest: could not move vote file {f.name}: {e}", file=sys.stderr)


def drain_votes(db, base):
    """Apply pending vote spool files (written by the web inbox) to the DB.

    Files are processed in name order (epoch-ms prefixed), so the newest vote
    for a slug lands last and wins. Bad files quarantine to failed/ and never
    crash the drain. Returns the number of votes applied.
    """
    root, processed, failed = _votes_dirs(base)
    applied = 0
    for f in sorted(root.glob("*.json")):
        try:
            entry = json.loads(f.read_text())
            slug = entry["slug"]
            vote = entry["vote"]
            note = entry.get("note") or None
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            _move_spool_file(f, failed)
            print(f"job_ingest: malformed vote file {f.name} -> failed/",
                  file=sys.stderr)
            continue
        try:
            row = db.resolve(slug)
        except Exception:
            row = None
        if not row:
            _move_spool_file(f, failed)
            print(f"job_ingest: vote for unknown job '{slug}' -> failed/",
                  file=sys.stderr)
            continue
        try:
            db.set_vote(row["uid"], vote, note=note)
        except ValueError as e:
            _move_spool_file(f, failed)
            print(f"job_ingest: vote rejected for {slug}: {e} -> failed/",
                  file=sys.stderr)
            continue
        _move_spool_file(f, processed)
        applied += 1
    return applied


def _blank_record(entry):
    return {
        "id": entry["id"], "url": entry.get("url", ""), "note": entry.get("note", ""),
        "submitted_at": entry.get("submitted_at", ""), "updated_at": _now_iso(),
        "status": "error", "uid": None, "company": None, "title": None,
        "llm_fit_score": None, "llm_rationale": None, "state": None,
        "package_files": [], "message": "", "gate_decision": None,
    }


# ATS vendors that give every customer its own subdomain. On these hosts the
# COMPANY is the leftmost label and the registered domain is only the vendor, so
# the generic "second-to-last label" rule picks the vendor by mistake
# (onecall.avature.net is One Call, not Avature).
_ATS_VENDOR_DOMAINS = {
    "avature.net", "icims.com", "myworkdayjobs.com", "bamboohr.com",
    "applytojob.com", "breezy.hr", "workable.com", "recruitee.com",
    "teamtailor.com", "phenompeople.com", "jobvite.com", "taleo.net",
    "successfactors.com", "ultipro.com", "paylocity.com", "dayforcehcm.com",
    "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com",
}


def _host_company_slug(url):
    """Best-effort company slug from a URL host, e.g. apply.acme-robotics.com -> acme-robotics."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return ""
    generic = {"www", "apply", "jobs", "job", "careers", "career", "boards",
               "recruiting", "talent", "work"}
    labels = [l for l in host.split(".") if l and l not in generic]
    if len(labels) >= 3 and ".".join(labels[-2:]) in _ATS_VENDOR_DOMAINS:
        return labels[0]
    if len(labels) >= 2:
        return labels[-2]
    return labels[0] if labels else ""


def _manual_ext_id(seed):
    """Stable short id for a manually-submitted (unparseable) posting."""
    return hashlib.sha1((seed or "manual").encode()).hexdigest()[:10]


def _already_handled(rec, existing, uid, url, discord_fn, webhook):
    """Fill a processed record for a posting already past 'queued' and ping Discord."""
    rec["uid"] = uid
    rec["company"] = existing["company"]
    rec["title"] = existing["title"]
    rec["state"] = existing["state"]
    rec["llm_fit_score"] = existing["llm_fit_score"]
    rec["llm_rationale"] = existing["llm_rationale"]
    rec["status"] = "scored"
    rec["message"] = (f"Already in the pipeline (state={existing['state']}); "
                      "skipped re-evaluation.")
    discord_fn(webhook,
               f"ℹ️ {existing['title']} @ {existing['company']} already at "
               f"'{existing['state']}' (skipping re-evaluation). {url}")
    return rec


def process_one(entry, db, *, master, api_key, webhook, master_path=None,
                verdict_fn=None, generate_fn=None, gate_fn=None,
                discord_fn=None, threshold=90):
    verdict_fn = verdict_fn or fit.verdict
    generate_fn = generate_fn or job_generate.generate
    gate_fn = gate_fn or gate.run_gate
    discord_fn = discord_fn or notify.post_discord

    rec = _blank_record(entry)
    url = entry.get("url", "")
    jd_pasted = (entry.get("jd") or "").strip()
    parsed = parse_posting_url(url)
    resolved = None
    meta = None

    if parsed:
        ats, company, ext_id = parsed["ats"], parsed["company"], parsed["ext_id"]
        uid = make_job_uid(ats, company, ext_id)

        # Idempotency guard, BEFORE any fetch or DB write: a posting already past
        # 'queued' has been handled, so a re-submission must not re-fetch, re-score,
        # re-generate, or disturb human progress.
        existing = db.get(uid)
        if existing is not None and existing["state"] not in ("discovered", "queued"):
            return _already_handled(rec, existing, uid, url, discord_fn, webhook)

        try:
            meta = fetch_posting_meta(parsed)
            fetch_err = ""
        except Exception as e:  # network / API error, isolated per-item
            meta, fetch_err = None, str(e)
        description = ((meta or {}).get("description") or "").strip()
        if not description and jd_pasted:
            description = jd_pasted  # supported ATS but unfetchable: use the pasted JD
        if not description:
            rec["status"] = "fetch_failed"
            rec["message"] = (
                (f"Couldn't fetch the JD ({fetch_err}). " if fetch_err
                 else "The posting returned an empty description. ")
                + "Paste the JD to evaluate it anyway, or add it via job-hound.")
            discord_fn(webhook,
                       f"⚠️ Job ingest: couldn't fetch {url}. Paste the JD or add it by hand.")
            return rec
        title = (((meta or {}).get("title") or "").strip()
                 or (entry.get("title") or "").strip() or f"{company} role")
        location = ((meta or {}).get("location") or "").strip()
    else:
        # Unparseable URL. A pasted JD wins outright (the human already did
        # the work, and re-fetching would burn a network call for less). With
        # no JD, try web resolution: LinkedIn guest view or JSON-LD JobPosting.
        resolved, web_err = None, ""
        if not jd_pasted:
            try:
                resolved = job_fetch.resolve_url(url)
            except job_fetch.FetchError as e:
                web_err = str(e)
        if resolved:
            ats, company, ext_id = (resolved["ats"], resolved["company"],
                                    resolved["ext_id"])
            # Adopt the canonical resolved URL (a chained ATS link, say) before
            # the idempotency check so an already-handled ping reports it, not
            # the raw submitted link.
            url = resolved["url"] or url
            uid = make_job_uid(ats, company, ext_id)
            existing = db.get(uid)
            if existing is not None and existing["state"] not in ("discovered", "queued"):
                return _already_handled(rec, existing, uid, url, discord_fn, webhook)
            title = resolved["title"] or f"{company} role"
            location = resolved["location"]
            description = resolved["description"]
            if not description:
                rec["status"] = "fetch_failed"
                rec["message"] = ("The posting resolved but returned no "
                                  "description. Paste the JD to evaluate it anyway.")
                discord_fn(webhook,
                           f"⚠️ Job ingest: no JD text at {url}. Paste the JD.")
                return rec
        elif not jd_pasted:
            rec["status"] = "fetch_failed"
            rec["message"] = (
                f"Couldn't resolve the posting ({web_err}). Auto-fetch covers "
                "Greenhouse, Lever, Ashby, SmartRecruiters, LinkedIn, and pages "
                "with JobPosting metadata. Paste the JD to evaluate it anyway, "
                "or add it via job-hound.")
            discord_fn(webhook,
                       f"⚠️ Job ingest: couldn't resolve {url}. Paste the JD or add it by hand.")
            return rec
        else:
            ats = "manual"
            company = ((entry.get("company") or "").strip()
                       or _host_company_slug(url) or "manual")
            ext_id = _manual_ext_id(url or (entry.get("title") or "") or jd_pasted[:64])
            uid = make_job_uid(ats, company, ext_id)
            existing = db.get(uid)
            if existing is not None and existing["state"] not in ("discovered", "queued"):
                return _already_handled(rec, existing, uid, url, discord_fn, webhook)
            title = (entry.get("title") or "").strip() or "Manual submission"
            location = ""
            description = jd_pasted

    dated = (meta if parsed else resolved) or {}
    job = {
        "ats": ats, "company": company, "id": ext_id,
        "title": title, "location": location, "url": url,
        "source": SOURCE,
        # The date comes from whichever path actually fetched the posting: the
        # parsed-ATS metadata, or the generic resolve. The parsed branch used
        # to hardcode "" here, so a Workday URL submitted through Mission
        # Control landed undated while the SAME url through `jh fetch` landed
        # dated, and the two ingest paths are supposed to agree. Only Workday's
        # metadata fetcher reports a date today; the rest fall back to
        # "mission-control", the label for "no real provenance".
        "posted_at": dated.get("posted_at", ""),
        "date_source": dated.get("date_source") or "mission-control",
    }
    db.upsert_job(job)
    db.set_fields(uid, description=description)
    row = db.get(uid)
    if row["state"] == "discovered":
        db.set_state(uid, "queued", note="mission-control ingest")
        row = db.get(uid)

    rec["uid"] = uid
    rec["company"] = company
    rec["title"] = title

    history = fit.build_history(db)
    # fit.verdict reads company/title/location via dict .get(); a sqlite3.Row has
    # no .get(), so pass a plain dict here. generate_fn below gets the Row (it uses
    # row["slug"] subscript access).
    job_for_verdict = {"company": row["company"], "title": row["title"],
                       "location": row["location"]}
    v = verdict_fn(job_for_verdict, master, history, api_key, jd_text=description)
    score = int(v["llm_fit_score"])
    rationale = v.get("llm_rationale", "")
    rec["llm_fit_score"] = score
    rec["llm_rationale"] = rationale
    db.set_fields(uid, llm_fit_score=score, llm_rationale=rationale,
                  llm_coding_bar=v.get("llm_coding_bar", ""))

    if score >= threshold:
        # The ingest path is an artifact path, so it must run the gate before
        # auto-drafting, exactly like the CLI will. jd_text is the description
        # already fetched above, so the gate does not re-fetch it.
        gated = gate_fn(db, db.get(uid), master, api_key=api_key, jd_text=description)
        decision = gated["decision"]
        rec["gate_decision"] = decision
        # RECOMMEND is a stronger pass than PROCEED (a strong match, no gaps), so
        # it must auto-draft too, otherwise the best-matching leads silently stop
        # generating a package.
        if decision in (gate.PROCEED, gate.RECOMMEND):
            # Pass master_path + api_key so generation uses the SAME master resume the
            # verdict scored against, not job_generate's bundled default.
            result = generate_fn(db, db.get(uid), master_path=master_path, api_key=api_key)
            db.set_state(uid, "drafted", note=f"auto-draft v{result.get('version')}")
            rec["package_files"] = [
                {"kind": kind, "path": str(p)} for kind, p in (result.get("files") or {}).items()
            ]
            rec["state"] = "drafted"
            rec["status"] = "drafted"
            discord_fn(webhook,
                       f"✅ {job['title']} @ {job['company']} scored {score} "
                       f"(packages ready for review). Apply: {url}")
        else:
            # Scored well but the gate says no: report why instead of drafting.
            rec["state"] = row["state"]
            rec["status"] = "gate_blocked"
            rec["message"] = (
                f"Scored {score} (cleared {threshold}) but the fit gate returned "
                f"{decision}; auto-draft withheld. See {gated.get('report_path')}.")
            discord_fn(webhook,
                       f"🚧 {job['title']} @ {job['company']} scored {score} but the "
                       f"fit gate returned {decision} (auto-draft withheld). "
                       f"See {gated.get('report_path')}. {url}")
    else:
        # Below threshold: move out of the draftable pool so `job next`/`job draft`
        # do not treat it as pursue-worthy work. skipped is reversible (un-skip to
        # queued) if the human disagrees.
        db.set_state(uid, "skipped",
                     note=f"mission-control auto-eval score {score} < {threshold}")
        db.set_fields(uid,
                      skip_reason=f"auto-eval score {score} < {threshold} (mission-control)")
        rec["state"] = "skipped"
        rec["status"] = "scored"
        rec["message"] = f"Scored {score} (below {threshold}); moved to skipped."
        discord_fn(webhook,
                   f"ℹ️ {job['title']} @ {job['company']} scored {score} "
                   f"(below {threshold}, skipping auto-draft). {url}")
    return rec


def main():
    inbox = os.environ.get("JOB_INBOX_DIR") or str(
        Path.home() / ".job-hound" / "job-inbox")
    try:
        db_path = resolve_db_path()
    except DBPathError as e:
        sys.exit(str(e))
    db = JobDB(db_path)
    # Votes need no API access; drain them even when the key is missing.
    drain_votes(db, inbox)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("job_ingest: ANTHROPIC_API_KEY not set; aborting", file=sys.stderr)
        return 1
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    master_path = Path(os.environ.get("JOB_MASTER") or
                       (Path(__file__).resolve().parent / "master_resume.yaml"))
    master = yaml.safe_load(master_path.read_text())
    pending = read_pending(inbox)
    for pfile, entry in pending:
        if not (isinstance(entry, dict) and entry.get("id") and entry.get("url")):
            # Malformed JSON or a submission missing id/url: record an error so it
            # is observable, then clear it so it does not linger in pending.
            rec = _blank_record({"id": Path(pfile).stem})
            rec["status"] = "error"
            rec["message"] = "Malformed or incomplete submission file; discarded."
            write_processed(inbox, rec)
            remove_pending(pfile)
            continue
        try:
            rec = process_one(entry, db, master=master, master_path=master_path,
                              api_key=api_key, webhook=webhook)
        except Exception as e:  # last-resort isolation
            rec = _blank_record(entry)
            rec["status"] = "error"
            rec["message"] = str(e)
            if webhook:
                notify.post_discord(webhook, f"⚠️ Job ingest error for {entry.get('url')}: {e}")
        write_processed(inbox, rec)
        remove_pending(pfile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
