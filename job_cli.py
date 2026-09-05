#!/usr/bin/env python3
"""
job_cli.py - Command surface for the job pipeline.

Verbs over the SQLite system of record (jobdb.py), with discovery wired in
from job_monitor.run_scan. Generation and packaging (stages 3-4) plug in
later behind `draft`.

  job scan                       run a discovery scan, ingest new jobs
  job list [--state STATE]       show jobs, optionally filtered
  job prune [--apply]            check postings for liveness, skip dead ones
  job show IDENT                 full detail + file + history for one job
  job queue IDENT                mark a job as one you'll pursue
  job skip IDENT [--note ...]    drop a job from consideration
  job fetch URL                  ingest one posting by URL -> queued
  job next                       show the next job ready to apply
  job draft IDENT                (stub) generate tailored package -> drafted
  job ready IDENT                mark a drafted package reviewed/ready
  job apply IDENT                you submitted it -> applied (date stamped)
  job state IDENT STATE          set state directly (validated)
  job close IDENT --outcome ...  close with an outcome
  job rounds IDENT ["a,b,c"]     show or set a job's interview rounds
  job stage IDENT N|decision     move the interview marker
  job board [--open]             render live interview loops to a web page
  job stats                      pipeline counts

IDENT can be a full uid, a slug, or a unique slug prefix.

Config and db locations follow the same conventions as job_monitor.
"""

import argparse
import json
import os
import sys
import time
import webbrowser
from datetime import date, datetime
from pathlib import Path

import yaml

import jobdb
from jobdb import JobDB, TransitionError, OUTCOMES, make_job_uid
import board
import job_monitor as jm
import freshness as fr
import staleness as stl
import fit
import notify
import job_fetch
import gate
import liveness as lv

HERE = Path(__file__).resolve().parent

DEFAULT_MAX_AGE_HOURS = 30 * 24  # 30 days; older listings are usually filled
DEFAULT_LLM_TOP = 3


# One resolver for every entry point (see jobdb.resolve_db_path). It refuses
# rather than falling back to a per-user path, because a fallback that creates
# the file is how a second database appeared on a workstation and swallowed nine
# days of decisions before anyone noticed.
resolve_db_path = jobdb.resolve_db_path


def load_cfg(path):
    p = Path(path).expanduser()
    if not p.exists():
        sys.exit(f"Config not found: {p}")
    return yaml.safe_load(p.read_text())


def fmt_row(r, show_age=True, idle_label=None):
    score = fit.rank_key(dict(r))
    line = f"[{r['state']:>12}] [{score:>3}] {r['slug']}\n               {r['title']} @ {r['company']} ({r['location'] or 'n/a'})"
    if show_age:
        try:
            label = fr.freshness_label(r["posted_at"], r["date_source"])
        except (KeyError, IndexError, TypeError):
            label = "age unknown"
        # The staleness marker rides the age line rather than adding one, so a
        # stale row is no taller than any other. The word is uppercased to
        # catch the eye in a dense list (this output is piped as often as it
        # is read, so ANSI colour is not an option), but the unit stays
        # lowercase to match the freshness label sharing the line: a row
        # reading "posted 53d ago ... IDLE 24D" cases the same unit two ways.
        if idle_label:
            label += f" · {idle_label.replace('idle', 'IDLE', 1)}"
        line += f"\n               {label}"
    return line


def need(db, ident):
    try:
        r = db.resolve(ident)
    except TransitionError as e:
        sys.exit(str(e))
    if not r:
        sys.exit(f"No job matching '{ident}'")
    return r


# --- commands -------------------------------------------------------------

def scan_and_ingest(db, cfg, verbose=False, describe=None):
    """Discover, ingest, and upgrade Greenhouse dates. Returns a summary dict.

    Side-effect-free apart from the DB writes it is asked to make, so the CLI
    and the MCP server can both drive discovery through this one code path
    (the CLI prints the summary; the MCP returns it).

    `describe` is the JD-body fetcher used for the residency check; it defaults
    to job_generate.fetch_description and is injectable for tests.
    """
    new, all_matches, manual = jm.run_scan(cfg, seen=set(), verbose=verbose)
    # Name the source explicitly. The column's default is 'unknown' on purpose,
    # so every ingestion path has to say what it is rather than inheriting a
    # claim it never made.
    for j in all_matches:
        j["source"] = "scan"
    # Rows already present keep their classification; only those newly inserted
    # this run get the bounded per-job follow-up fetches below.
    new_uids = {jm.job_key(j) for j in all_matches
                if db.get(jm.job_key(j)) is None}
    added = db.ingest_scan(all_matches)
    # Upgrade Greenhouse postings to their true first_published date. The board
    # listing only gives updated_at (approximate). We do this only for newly
    # added candidates, so the extra calls are bounded by what passed filters.
    upgraded = 0
    for j in all_matches:
        if j["ats"] != "greenhouse":
            continue
        uid = f"{j['ats']}:{j['company']}:{j['id']}"
        row = db.get(uid)
        if not row or not (row["date_source"] or "").endswith("~"):
            continue
        iso, source = fr.upgrade_greenhouse_date(j["company"], j["id"])
        if iso:
            db.set_fields(uid, posted_at=iso, date_source=source)
            upgraded += 1
    # Body residency check: a role whose location field reads remote can still
    # require a non-Eastern time zone in the JD prose (e.g. "reside within the
    # Central Time Zone"). The structured-field classifier can't see that, so
    # for each newly-added remote role we re-read the fetched JD and quarantine
    # it ('verify') when it excludes Eastern. Bounded to new rows; a fetch error
    # must never break the scan.
    held = _hold_remote_by_body(db, new_uids, describe)
    return {"matches": len(all_matches), "added": added,
            "upgraded": upgraded, "location_held": held, "manual": manual}


def openjobs_and_ingest(db, cfg, top=None, groups=None, verbose=False,
                        discover=None):
    """Run the open-jobs wide net and ingest what is genuinely new.

    The reusable core for the second discovery source, mirroring
    scan_and_ingest: it returns a summary dict and does no printing, so the
    CLI, the MCP adapter and the web inbox can all drive one code path.

    Dedup runs BEFORE the cap, deliberately. Capping first would mean that on a
    day when the 15 best matches are all already in the pipeline, the run
    ingests nothing; the cap is a budget on NEW leads, the same way `list
    --limit` is a budget on discoveries.
    """
    import openjobs
    discover = discover or openjobs.discover
    top = openjobs.DEFAULT_TOP if top is None else top
    groups = openjobs.DEFAULT_GROUPS if groups is None else groups

    # discover() fails SAFE, so a corpus outage comes back as an empty list
    # rather than an exception. `problems` is how the reason escapes; without
    # it a 503 and a genuinely quiet day are the same observation.
    problems = []
    try:
        found = discover(cfg, groups=groups, verbose=verbose, problems=problems)
    except Exception as e:
        # discover() already fails safe on every path it knows about. This is
        # the backstop for the one it does not: daily.sh must reach the digest.
        summary = {"found": 0, "added": 0, "duplicate": 0, "capped": 0,
                   "error": f"{type(e).__name__}: {e}"}
        openjobs.write_status(summary)
        return summary

    known_urls = db.known_urls()

    fresh, duplicate = [], 0
    # Sort here rather than trusting discover() to have done it. The cap slices
    # the front of this list, so the ordering is load-bearing; leaving it as an
    # unwritten contract between two functions is the same shape as the
    # centroid bug that already made "top 15" mean "the first 15 rows of a
    # file" once.
    for j in sorted(found, key=lambda c: -c.get("sim", 0)):
        uid = jobdb.make_job_uid(j["ats"], j["company"], j["id"])
        url = jobdb.canonical_url(j.get("url"))
        if db.get(uid) is not None or (url and url in known_urls):
            duplicate += 1
            continue
        # Feed accepted URLs back in, so two spellings of the same posting
        # arriving in ONE run collapse too. known_urls is only a snapshot of
        # what was already stored.
        if url:
            known_urls.add(url)
        fresh.append(j)

    # `top` is a budget on rows written to the single system of record, so 0
    # means zero. `refine --top 0` means unlimited, but that is a display
    # limit; the two must not share a spelling when one of them writes.
    capped = fresh if top is None else fresh[:max(0, top)]
    added = db.ingest_scan(capped)
    summary = {"found": len(found), "added": added, "duplicate": duplicate,
               "capped": len(fresh) - len(capped),
               "error": "; ".join(problems) or None}
    # Recorded so the digest can say whether the wide net actually ran. A
    # silent empty result is indistinguishable from a quiet day.
    openjobs.write_status(summary)
    return summary


def _hold_remote_by_body(db, new_uids, describe=None):
    """Re-read the JD of each newly-added remote role; 'verify' if it excludes
    Eastern. Returns the count quarantined."""
    if describe is None:
        import job_generate
        describe = job_generate.fetch_description
    held = 0
    fetched = False
    for uid in new_uids:
        row = db.get(uid)
        if not row or (row["location_type"] or "") != "remote":
            continue
        if fetched:
            time.sleep(jm.SLEEP_BETWEEN_CALLS)  # polite delay between ATS fetches
        try:
            jd = describe(row)
        except Exception:
            continue  # network/parse hiccup: leave the role as-is
        finally:
            fetched = True
        if jd and jm.residency_excludes_eastern(jd):
            db.set_fields(uid, location_type="verify")
            held += 1
    return held


def cmd_scan(db, args):
    cfg = load_cfg(args.config)
    r = scan_and_ingest(db, cfg, verbose=True)
    print(f"\nScan complete. {r['matches']} matches, {r['added']} new -> discovered.")
    if r["upgraded"]:
        print(f"Resolved true posting dates for {r['upgraded']} Greenhouse role(s).")
    if r.get("location_held"):
        print(f"Held {r['location_held']} remote role(s) for location check "
              f"(non-Eastern time zone in the JD).")
    if r["manual"]:
        print(f"{len(r['manual'])} company(ies) need manual checking:")
        for m in r["manual"]:
            print(f"  - {m['name']} ({m['ats']}): {m.get('careers_url', 'n/a')}")


def cmd_openjobs(db, args):
    cfg = load_cfg(args.config)
    r = openjobs_and_ingest(db, cfg, top=args.top, groups=args.groups,
                            verbose=True)
    if r["error"]:
        print(f"Wide net unavailable ({r['error']}). Pipeline unchanged.")
        return
    print(f"\nWide net complete. {r['found']} candidates, "
          f"{r['duplicate']} already known, {r['added']} new -> discovered.")
    if r["capped"]:
        print(f"{r['capped']} more matched but were held back by the "
              f"--top {args.top} cap; they will rank again tomorrow.")


def _verify_filter(rows, show_all):
    """Split rows into (kept, hidden_count) by the location quarantine.

    Roles tagged location_type='verify' matched only via the US country name
    while pinning a specific onsite metro, so the location field may be
    mislabeling a remote role. They are kept in the DB but held out of default
    views; --all surfaces them for a human to check the JD.
    """
    if show_all:
        return rows, 0
    kept = [r for r in rows if (r["location_type"] or "") != "verify"]
    return kept, len(rows) - len(kept)


def _fresh_filter(rows, max_age_hours, show_all):
    """Split rows into (kept, hidden_count) by freshness policy.

    Undatable postings are KEPT (flagged 'age unknown'), never silently dropped.

    So are committed leads, at ANY posting age. Posting age triages the
    discovery firehose; it is not a reason to hide a lead the operator already decided
    to pursue. And it cannot be left in front of the staleness signal at all:
    a lead cannot be acted on before it was posted, so idle days can never
    exceed posting age, which puts every idle committed lead on the wrong side
    of this filter exactly as the problem gets worse. The exemption covers the
    whole committed set rather than only the leads already past the idle
    threshold, so rows do not appear and disappear as the idle clock crosses
    7 days. The web inbox's passesFreshFilter (lib/job-sort.ts) exempts the
    same set; these are two surfaces of one feature and they have to agree.
    """
    if show_all:
        return rows, 0
    kept = []
    hidden = 0
    for r in rows:
        if r["state"] in stl.COMMITTED_STATES:
            kept.append(r)
            continue
        try:
            ok = fr.passes_max_age(r["posted_at"], r["date_source"],
                                   max_age_hours, keep_unknown=True)
        except (KeyError, IndexError, TypeError):
            ok = True
        if ok:
            kept.append(r)
        else:
            hidden += 1
    return kept, hidden


def _apply_limit(rows, limit):
    """Cut `rows` to `limit`, spending the budget on non-committed leads only.

    A committed lead always passes and never spends budget, so the result can
    exceed `limit` by the size of the committed set. Same reasoning as the
    exemption in _fresh_filter: a limit bounds the discovery firehose, and a
    lead the operator already decided to pursue is not firehose. Without this, a caller
    bounding its own response (the MCP path, where an agent will plausibly pass
    limit=20) drops exactly the committed leads the freshness exemption just
    stopped hiding. The web inbox's applyRowLimit (lib/job-hound.ts) is this
    rule; these are surfaces of one feature and they have to agree.
    """
    if not limit:
        return rows
    budget = limit
    kept = []
    for r in rows:
        if r["state"] in stl.COMMITTED_STATES:
            kept.append(r)
            continue
        if budget <= 0:
            continue
        budget -= 1
        kept.append(r)
    return kept


def _hidden_stale(before, after, idle):
    """How many of the rows a filter dropped are stale committed leads."""
    shown = {r["uid"] for r in after}
    return sum(1 for r in before
               if r["uid"] not in shown and idle.get(r["uid"]))


def _stale_note(n):
    """Trailer clause announcing stale leads a filter hid, or '' for none.

    The location quarantine is the only filter that can still hide one:
    _fresh_filter exempts committed leads, so its hidden set never contains a
    stale lead and never needs this clause.
    """
    if not n:
        return ""
    return f", {n} of them idle {stl.STALE_AFTER_DAYS}d or more"


def cmd_list(db, args):
    rows = db.list(state=args.state, limit=None)
    # Idle labels are computed from the UNFILTERED rows, deliberately above
    # both filters, the same reason refine_pipeline does it (see the comment
    # there). _fresh_filter no longer drops committed leads at all, so no
    # posting age can silence the marker. The location quarantine still can,
    # at any idle age, because `verify` says a field on the row may be wrong
    # rather than that the row is old; those get counted into the trailer
    # below instead.
    #
    # r["state"] is read bare here while fmt_row and _fresh_filter guard their
    # column reads. That is not an oversight: `state` is TEXT NOT NULL DEFAULT
    # 'discovered' in the jobs schema (jobdb.py), so every row db.list() can
    # return has one. The guards elsewhere cover `posted_at` and `date_source`,
    # which are nullable and were added by migration, so an old row or a
    # narrower SELECT can genuinely be missing them. Wrapping this line too
    # would only hide a real bug: if `state` is absent, the row did not come
    # from the jobs table and the caller is wrong.
    activity = db.last_activity(uids=[r["uid"] for r in rows])
    idle = {r["uid"]: stl.staleness_label(r["state"], activity.get(r["uid"]))
            for r in rows}

    kept, verify_hidden = _verify_filter(rows, args.all)
    verify_hidden_stale = _hidden_stale(rows, kept, idle)
    rows, hidden = _fresh_filter(kept, args.max_age, args.all)

    rows = sorted(rows, key=lambda r: fit.sort_key(dict(r)), reverse=True)
    rows = _apply_limit(rows, args.limit)
    if not rows:
        msg = "No jobs." if not args.state else f"No jobs. (state={args.state})"
        if hidden:
            msg += (f" ({hidden} hidden as older than {args.max_age/24:.0f}d"
                    "; --all to show)")
        if verify_hidden:
            msg += (f" ({verify_hidden} held for location check"
                    f"{_stale_note(verify_hidden_stale)}; --all to show)")
        print(msg)
        return
    for r in rows:
        print(fmt_row(r, idle_label=idle.get(r["uid"])))
    if hidden:
        print(f"\n({hidden} older than {args.max_age/24:.0f}d hidden"
              "; --all to show them)")
    if verify_hidden:
        print(f"({verify_hidden} onsite-metro lead(s) held for location check"
              f"{_stale_note(verify_hidden_stale)}; --all to show them)")


def cmd_show(db, args):
    r = need(db, args.ident)
    print(f"slug    : {r['slug']}")
    print(f"uid     : {r['uid']}")
    print(f"title   : {r['title']}")
    print(f"company : {r['company']} ({r['ats']})")
    print(f"location: {r['location'] or 'n/a'}")
    print(f"state   : {r['state']}" + (f" / {r['outcome']}" if r['outcome'] else ""))
    try:
        print(f"posted  : {fr.freshness_label(r['posted_at'], r['date_source'])}")
    except (KeyError, IndexError, TypeError):
        pass
    print(f"url     : {r['url']}")
    if r['folder']:
        print(f"folder  : {r['folder']}")
    files = db.files_for(r['uid'])
    if files:
        print("files   :")
        for f in files:
            print(f"          {f['kind']} v{f['version']}: {f['path']}")
    hist = db.history(r['uid'])
    if hist:
        print("history :")
        for h in hist:
            line = f"          {h['at']}  {h['from_state'] or '-'} -> {h['to_state']}"
            if h['note']:
                line += f"  ({h['note']})"
            print(line)


def _transition(db, ident, state, note=None, outcome=None, reason=None):
    r = need(db, ident)
    try:
        updated = db.set_state(r['uid'], state, note=note, outcome=outcome,
                               reason=reason)
    except TransitionError as e:
        sys.exit(str(e))
    print(f"{r['slug']}: {r['state']} -> {updated['state']}")


def _load_master(args):
    p = fit.resolve_config_path("master_resume.yaml", getattr(args, "master", None))
    return yaml.safe_load(p.read_text())


def _print_gate(out):
    label = gate._DECISION_LABEL.get(out["decision"], out["decision"])
    print(f"\nGate: {label}")
    if out.get("model"):
        print(f"  model: {out['model']}")
    print(f"  {out['counts']['known_hard_none']} hard requirement(s) with no evidence")
    if out["counts"]["unresolved"]:
        print(f"  {out['counts']['unresolved']} item(s) need your ruling "
              f"(jh gate-rule)")
    if out["title"].get("mismatch"):
        print(f"  TITLE MISMATCH: {out['title']['note']}")
    print(f"  Report: {out['report_path']}")
    if out["decision"] == gate.DO_NOT_APPLY:
        print("\n  Drafting is BLOCKED. Read the report before you argue with it.")


def cmd_gate(db, args):
    r = need(db, args.ident)
    print(f"Gating: {r['title']} @ {r['company']}")
    print("Reading the full JD and checking it against your evidence...")
    out = gate.run_gate(db, r, _load_master(args))
    _print_gate(out)


def cmd_gate_rule(db, args):
    """Adjudicate one UNSURE item. Recomputes with no API call.

    Only a genuinely UNSURE classification is adjudicable: a confident gate
    verdict was never meant to be arguable, and a do_not_claim requirement is
    absolute and cannot be argued with by the model or by a human ruling. That
    holds however the ledger matched, by substring or by meaning: a
    semantic-screen hit is the same ledger read a second way, so letting it be
    ruled SOFT would reopen the exact hole the ledger closes. Editing
    master_resume.yaml and re-gating is the way through, or gate-override.
    A written note is mandatory and every ruling is audited to state_log, the
    same rule gate-override and gap-close already follow.
    """
    r = need(db, args.ident)
    if not (args.note or "").strip():
        sys.exit("ruling on a requirement requires a written note. "
                 "If you cannot write one, that is the answer.")
    stored = json.loads(r["gate_json"] or "{}")
    reqs = stored.get("requirements") or []
    if not (1 <= args.n <= len(reqs)):
        sys.exit(f"no requirement {args.n}; the report lists 1..{len(reqs)}")
    req = reqs[args.n - 1]
    if gate.forced_by_ledger(req):
        how = ("matched to your do_not_claim list by meaning (the semantic screen)"
               if (req.get("forced") or "").startswith(gate.SCREEN_FORCED)
               else "on your do_not_claim list")
        sys.exit(f"requirement {args.n} is {how}. The ledger is not "
                 f"adjudicable. If this is wrong, edit do_not_claim in master_resume.yaml "
                 f"and re-run: jh gate {r['slug']}")
    if req.get("ruled_by_human") or req.get("confidence") != "low":
        sys.exit(f"requirement {args.n} is not UNSURE (the gate was confident). Only an "
                 f"UNSURE classification can be ruled on. To proceed against a confident "
                 f"gate, use: jh gate-override {r['slug']} --reason \"...\"")
    old_hard = req["hard"]
    req["hard"] = bool(args.hard) and not bool(args.soft)
    req["confidence"] = "high"
    req["ruled_by_human"] = True
    note = args.note.strip()
    # Why the operator ruled this way. Kept on the requirement so the rulings accumulate
    # as a corpus we can feed back into the prompt later.
    req["ruling_note"] = note
    db.audit_gate_rule(r["uid"], args.n, old_hard, req["hard"], note)
    # Carry the ruling into recompute without a stale set_gate write first:
    # recompute() persists exactly once, with the freshly recomputed decision,
    # not the pre-ruling one that was on the row a moment ago.
    updated = dict(r)
    # Preserve every key run_gate persisted (location, skills_decision), not just
    # requirements+title. Dropping "location" would make recompute lose the
    # NOT_REMOTE overlay and flip a non-remote job back to a pass on the next ruling.
    updated["gate_json"] = json.dumps({
        **stored,
        "requirements": reqs,
        "title": stored.get("title", {}),
    })
    out = gate.recompute(db, updated)
    print(f"Ruled #{args.n} {'HARD' if req['hard'] else 'SOFT'}.")
    _print_gate(out)


def cmd_gate_override(db, args):
    r = need(db, args.ident)
    if not (args.reason or "").strip():
        sys.exit("an override requires a written reason. "
                 "If you cannot write one, that is the answer.")
    if not r["gate_decision"]:
        sys.exit(f"{r['slug']} has never been gated, so there is no decision "
                  f"to override. Run: jh gate {r['slug']}")
    db.set_override(r["uid"], args.reason.strip())
    print(f"{r['slug']}: gate overridden. Reason recorded and audited.")
    print("Drafting is now allowed.")


def cmd_gaps(db, args):
    rows = (db.gaps_for(need(db, args.ident)["uid"]) if args.ident
            else db.open_gaps())
    if not rows:
        print("No open gaps.")
        return
    for g in rows:
        planned = "planned" if (g["plan"] and g["hours_estimate"]
                                and g["deadline"]) else "UNPLANNED"
        print(f"[{g['id']:>3}] [{planned}] {g['requirement']}")
        if g["plan"]:
            print(f"       plan: {g['plan']} ({g['hours_estimate']}h, "
                  f"by {g['deadline']})")
    if any(not (g["plan"] and g["hours_estimate"] and g["deadline"]) for g in rows):
        print("\nPlan a gap: jh gap-plan <id> --plan \"...\" --hours N "
              "--deadline YYYY-MM-DD")


def cmd_gap_plan(db, args):
    if args.hours < 1:
        sys.exit("an hours estimate must be at least 1. A zero-hour plan is not a plan.")
    try:
        datetime.strptime(args.deadline, "%Y-%m-%d")
    except ValueError:
        sys.exit(f"deadline must be a real date in YYYY-MM-DD form, got {args.deadline!r}")
    n = db.plan_gap(args.gap_id, args.plan, args.hours, args.deadline)
    if not n:
        sys.exit(f"no gap with id {args.gap_id}. Run: jh gaps")
    print(f"gap {args.gap_id} planned: {args.hours}h by {args.deadline}")


def cmd_gap_close(db, args):
    if not (args.reason or "").strip():
        sys.exit("closing a gap requires a written reason. "
                 "If you cannot write one, that is the answer.")
    n = db.close_gap(args.gap_id, args.reason.strip())
    if not n:
        sys.exit(f"no gap with id {args.gap_id}. Run: jh gaps")
    print(f"gap {args.gap_id} closed. Reason recorded and audited.")


def cmd_rounds(db, args):
    """Show or edit a job's ordered round list."""
    r = need(db, args.ident)
    if args.add:
        labels = jobdb.rounds_of(r) or list(jobdb.DEFAULT_ROUNDS)
        labels.append(args.add)
    elif args.labels:
        labels = [s for s in args.labels.split(",")]
    else:
        current = jobdb.rounds_of(r)
        if not current:
            n = len([l for l in jobdb.DEFAULT_ROUNDS if l != "recruiter"])
            print(f"{r['slug']}: no rounds set "
                  f"(defaults to a recruiter screen and {n} unnamed rounds)")
            return
        # The number is the LIST POSITION, which is what `jh stage` takes. An
        # unfilled placeholder is printed as unnamed rather than verbatim: rows
        # staged before the seed lost its number still carry "round 1".."round
        # 3", and printing one beside its position put two disagreeing numbers
        # on one line. This is the third surface, and there is no migration.
        for i, label in enumerate(current, start=1):
            print(f"  {i}. {'(unnamed)' if board.is_placeholder(label) else label}")
        return
    try:
        updated = db.set_rounds(r["uid"], labels)
    except ValueError as e:
        sys.exit(str(e))
    print(f"{r['slug']}: " + ", ".join(jobdb.rounds_of(updated)))


def cmd_stage(db, args):
    """Move the marker, transitioning out of `applied` when that is where it is.

    A screen that actually happened is one command, not two: the job moves to
    interviewing and the marker lands in the same call, each audited on its own
    state_log row.
    """
    r = need(db, args.ident)
    if r["state"] not in ("applied", "interviewing"):
        sys.exit(f"{r['slug']} is {r['state']}, so there is no live "
                 f"conversation to stage. Only applied or interviewing jobs "
                 f"can carry a round marker.")
    decision = args.round == "decision"
    at = None
    if not decision:
        try:
            at = int(args.round)
        except ValueError:
            sys.exit(f"round must be a number or 'decision', got {args.round!r}")

    # The marker write goes FIRST and the lifecycle transition second, because
    # every remaining check (a `--on` date that is malformed or in the future,
    # a round outside this job's list) lives inside set_stage. Transitioning
    # first meant a command that then exited non-zero had already committed an
    # audited applied -> interviewing to state_log with no marker to show for
    # it, and nothing later would ever repair that. Ordering, not a
    # transaction: the two writes are separate commits either way, and this is
    # the order where the survivable failure is the one that can happen.
    try:
        updated = db.set_stage(r["uid"], at=at, decision=decision,
                               next_note=args.next,
                               occurred=getattr(args, "on", None))
    except ValueError as e:
        sys.exit(str(e))

    if r["state"] == "applied":
        try:
            db.set_state(r["uid"], "interviewing", note="stage set")
        except TransitionError as e:
            sys.exit(str(e))
        print(f"{r['slug']}: applied -> interviewing")

    # Read the caption off the same derivation the board draws, rather than
    # echoing the stored label. Positions are 1-based over the whole list and
    # captions count only the real rounds, so `jh stage <ident> 2` used to
    # answer with a label naming a different number than the node the board
    # drew for it. One numbering, in one place.
    rounds = jobdb.rounds_of(updated)
    cap, detail = board.captions(rounds + ["decision"])[
        len(rounds) if decision else at - 1]
    label = f"{cap} ({detail})" if detail else cap
    print(f"{r['slug']}: at {label}")
    if updated["interview_next"]:
        print(f"  next: {updated['interview_next']}")


def cmd_board(db, args):
    rows = db.interviewing()
    out = Path(args.out).expanduser() if args.out else (
        Path(os.environ.get("JOB_APPS_DIR",
                            Path.home() / "job-applications")).expanduser()
        / "interviews.html")
    board.write(rows, out)
    print(f"{len(rows)} live conversation(s) -> {out}")
    if not rows:
        print("Nothing is in the interviewing state. "
              "Set one with: jh stage <ident> <n>")
    if args.open:
        webbrowser.open(out.resolve().as_uri())


def cmd_queue(db, args):
    r = need(db, args.ident)
    _transition(db, args.ident, "queued", note=args.note)
    out = gate.run_gate(db, db.get(r["uid"]), _load_master(args))
    _print_gate(out)


def cmd_skip(db, args):
    _transition(db, args.ident, "skipped", note=args.note,
                reason=getattr(args, "reason", None))


def cmd_prune(db, args):
    """Sweep postings for liveness, and with --apply skip the dead ones.

    Dry run by default: a sweep that mutates on first contact is a sweep
    nobody runs. Only `closed` is ever acted on. `unknown` means the check
    could not tell (no public endpoint, a network error, a 5xx), and a lead
    we could not tell about keeps its place in the pipeline, because marking
    a live posting skipped removes a real opportunity and nothing downstream
    would surface it again.

    Sweeps oldest first. db.list orders newest first, which would point
    --limit at the leads most likely to still be open; the dead ones are the
    old ones. The re-order and the slice both happen here rather than in
    db.list, whose newest-first contract every other caller depends on.
    """
    rows = sorted(db.list(state=args.state), key=lambda r: r["discovered_at"] or "")
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        print(f"No jobs. (state={args.state})")
        return
    counts = {lv.OPEN: 0, lv.CLOSED: 0, lv.UNKNOWN: 0}
    marked = 0
    classifier_errors = 0
    checked_on = date.today().isoformat()
    print(f"Checking {len(rows)} posting(s) in '{args.state}'...")
    for i, r in enumerate(rows):
        if i:
            time.sleep(jm.SLEEP_BETWEEN_CALLS)  # polite delay between ATS calls

        def _note_classifier_error(slug=r["slug"]):
            nonlocal classifier_errors
            classifier_errors += 1
            if classifier_errors == 1:
                print(f"  classifier error on {slug}: the payload classifier "
                      "raised unexpectedly (verdict is still unknown)",
                      file=sys.stderr)

        verdict = lv.check(r, on_classifier_error=_note_classifier_error)
        counts[verdict] += 1
        if verdict != lv.CLOSED:
            continue
        label = f"{r['slug']}  ({r['title']} @ {r['company']})"
        if not args.apply:
            print(f"  would skip: {label}")
            continue
        try:
            db.set_state(r["uid"], "skipped",
                         note=f"posting closed (checked {checked_on})")
        except TransitionError as e:
            print(f"  could not skip {r['slug']}: {e}")
            continue
        marked += 1
        print(f"  skipped: {label}")
    print(f"\n{len(rows)} checked: {counts[lv.OPEN]} open, "
          f"{counts[lv.CLOSED]} closed, {counts[lv.UNKNOWN]} unknown.")
    if classifier_errors:
        print(f"{classifier_errors} classifier error(s) among the unknowns: "
              "the payload classifier raised unexpectedly. See stderr.")
    if args.apply:
        print(f"{marked} marked skipped. Unknowns were left alone.")
    elif counts[lv.CLOSED]:
        print("Dry run, nothing changed. Re-run with --apply to mark them skipped.")


def cmd_ready(db, args):
    _transition(db, args.ident, "ready", note=args.note)


def cmd_apply(db, args):
    _transition(db, args.ident, "applied", note=args.note)


def cmd_state(db, args):
    _transition(db, args.ident, args.state, note=args.note)


def cmd_close(db, args):
    _transition(db, args.ident, "closed", note=args.note, outcome=args.outcome,
                reason=getattr(args, "reason", None))


def cmd_next(db, args):
    r = db.next_to_apply()
    if not r:
        nd = db.next_to_draft()
        if nd:
            print("Nothing ready to apply yet.")
            print(f"Next to draft: {nd['slug']}  ({nd['title']} @ {nd['company']})")
            print(f"Run: job draft {nd['slug']}")
        else:
            print("Queue is empty. Run `job scan`, then `job queue <ident>`.")
        return
    print("Next to apply:\n")
    print(fmt_row(r))
    print(f"\n  Apply at: {r['url']}")
    if r['folder']:
        print(f"  Package : {r['folder']}")
    print(f"\nWhen submitted: job apply {r['slug']}")


def cmd_draft(db, args):
    r = need(db, args.ident)
    if r["state"] not in ("queued", "drafted"):
        sys.exit(f"{r['slug']} is '{r['state']}'. Queue it first: job queue {r['slug']}")
    try:
        import job_generate
    except ImportError as e:
        sys.exit(f"generator unavailable: {e}")
    print(f"Drafting package for: {r['title']} @ {r['company']}")
    print("Fetching job description and calling the model (this takes a moment)...")
    try:
        result = job_generate.generate(db, r, master_path=args.master)
    except gate.GateBlocked as e:
        sys.exit(f"\nBLOCKED by the fit gate.\n\n{e}\n")
    except Exception as e:
        sys.exit(f"draft failed: {e}")
    # queued -> drafted (or stay drafted on a re-draft)
    if r["state"] == "queued":
        db.set_state(r["uid"], "drafted", note=f"generated v{result['version']}")
    print(f"\nPackage v{result['version']} written to:\n  {result['folder']}")
    for label, path in result["files"].items():
        print(f"  {label}: {Path(path).name}")
    if not result["pdf"]:
        print("  (PDFs skipped: LibreOffice not found or JOB_PDF=off)")
    if result["tailoring_note"]:
        print(f"\nWhat was emphasized:\n  {result['tailoring_note']}")
    print(f"\nReview the package, then: job ready {r['slug']}")


def cmd_stats(db, args):
    counts = db.counts()
    if not counts:
        print("No jobs yet.")
        return
    order = ["discovered", "queued", "drafted", "ready",
             "applied", "interviewing", "closed", "skipped"]
    total = sum(counts.values())
    print(f"Pipeline ({total} total):")
    for s in order:
        if s in counts:
            print(f"  {s:>12}: {counts[s]}")


def cmd_fetch(db, args):
    """Ingest one posting by URL (LinkedIn, ATS, or JSON-LD page) -> queued."""
    try:
        r = job_fetch.resolve_url(args.url)
    except job_fetch.FetchError as e:
        print(f"fetch failed: {e}")
        print("Tip: submit it through the web inbox with the JD pasted, "
              "or add the company to companies.yaml and scan.")
        sys.exit(1)
    uid = make_job_uid(r["ats"], r["company"], r["ext_id"])
    existing = db.get(uid)
    if existing:
        print(f"already tracked: {existing['title']} @ {existing['company']} "
              f"[{existing['slug']}] state={existing['state']}")
        return
    db.upsert_job({"ats": r["ats"], "company": r["company"], "id": r["ext_id"],
                   "title": r["title"], "location": r["location"],
                   "url": r["url"], "posted_at": r["posted_at"],
                   "date_source": r["date_source"], "source": "fetch"})
    if r["description"]:
        db.set_fields(uid, description=r["description"])
    db.set_state(uid, "queued", note="fetched by url")
    row = db.get(uid)
    print(f"queued: {row['title']} @ {row['company']} [{row['slug']}]")
    if row["location"]:
        print(f"location: {row['location']}")
    out = gate.run_gate(db, db.get(uid), _load_master(args))
    _print_gate(out)
    if out["decision"] in (gate.PROCEED, gate.RECOMMEND):
        print(f"\nnext: jh draft {row['slug']}")


def refine_pipeline(db, *, profile, master, top=DEFAULT_LLM_TOP, no_llm=False,
                    max_age=DEFAULT_MAX_AGE_HOURS, show_all=False, api_key=None):
    """Score and rank the active pipeline, optionally run LLM verdicts on the
    fresh top-N, and build the ranked digest. Returns a result dict; performs
    no printing and pushes nowhere. Shared by the CLI and the MCP server.

    `api_key` None (or no_llm) means deterministic scoring only. Per-lead
    verdict failures are swallowed (counted against the budget) and reported in
    `verdict_failures` for the caller to surface as it sees fit.
    """
    # Rank the full active pipeline (every non-terminal lead), not just new ones.
    active = [dict(r) for r in db.list()
              if r["state"] not in ("closed", "skipped")]
    if not active:
        return {"digest": None, "shown_uids": [], "active": 0, "hidden": 0,
                "verify_hidden": 0, "verdict_failures": [], "stale": []}

    # 1. Deterministic score for every active lead (free, always re-run).
    # The do_not_claim ledger rides along as a demotion signal, so a lead the
    # gate is certain to refuse stops presenting as a top match. Read from the
    # SAME master the gate reads, so the ranker and the gate can never disagree
    # about what is forbidden. A malformed ledger falls back to un-demoted
    # scoring rather than breaking the unattended nightly digest; that is the
    # gate's error to report (loudly, as ERROR) and not the ranker's to raise.
    try:
        _caps, ledger = gate.load_profile(master)
    except gate.ProfileError:
        ledger = None
    for j in active:
        sc, reasons = fit.score(j, profile, do_not_claim=ledger)
        db.set_fields(j["uid"], fit_score=sc, fit_reasons=reasons)
        j["fit_score"] = sc

    # Computed from `active`, deliberately upstream of BOTH filters below.
    # The freshness filter drops old postings and step 3 keeps only
    # `discovered`, so a committed lead with an old posting would be invisible
    # to the digest. That is exactly how a drafted Omnicell package sat 24
    # days unsent, and this section exists to stop it recurring.
    # It runs after the scoring loop so the score the digest renders is the
    # fresh one because these dicts were scored, not because `stale` happens
    # to alias the same objects as `active`.
    # One batched query, not one per lead. Jobs with no recorded action are
    # absent from `activity`, so .get yields None, which reads as not stale.
    activity = db.last_activity(uids=[j["uid"] for j in active])
    stale = []
    for j in active:
        label = stl.staleness_label(j["state"], activity.get(j["uid"]))
        if label:
            # fit._stale_digest_line renders this key with no fallback, so it
            # must always be present and non-empty. Only a truthy label gets
            # here, which is what guarantees that.
            j["idle_label"] = label
            stale.append(j)

    # 2. Apply the same freshness/verify policy the list view uses.
    visible, verify_hidden = _verify_filter(active, show_all)
    fresh_rows, hidden = _fresh_filter(visible, max_age, show_all)

    # 3. The digest surfaces only leads not yet acted on. Leads already queued,
    #    drafted, or ready are in the working set and stay out of the digest.
    candidates = [j for j in fresh_rows if j["state"] == "discovered"]

    # 4. LLM verdict on the top-N discovered candidates by deterministic score.
    failures = []
    if not no_llm and api_key:
        history = fit.build_history(db)
        topn = sorted(candidates, key=lambda x: x["fit_score"], reverse=True)
        done = 0
        for j in topn:
            if done >= top:
                break
            if j.get("llm_fit_score") is not None:
                continue
            try:
                v = fit.verdict(j, master, history, api_key)
            except Exception as e:
                failures.append(f"{j['slug']}: {e}")
                done += 1
                continue
            db.set_fields(j["uid"], llm_fit_score=v["llm_fit_score"],
                          llm_rationale=v["llm_rationale"],
                          llm_coding_bar=v["llm_coding_bar"])
            j.update(v)
            done += 1

    # 5. Reload full rows (fresh verdicts + digested_at) and partition into
    #    never-sent (New) and already-sent (Still open).
    full = [dict(db.get(j["uid"])) for j in candidates]
    new = [j for j in full if not j.get("digested_at")]
    seen = [j for j in full if j.get("digested_at")]
    # deliver_limit is passed from here rather than imported inside fit.py:
    # fit.py renders text and should not know the transport, and job_cli is
    # already the layer that wires stages together. cmd_refine stamps
    # mark_digested(shown_uids) after a successful post, so shown_uids has to
    # mean "actually delivered", not "rendered into a string that was then
    # cut at 1900 characters".
    # The wide net runs as its own step in bin/daily.sh, a separate process, so
    # its outcome reaches the digest through the status file it leaves behind
    # rather than through a return value. Absent, the digest is unchanged.
    import openjobs
    digest, shown_uids = fit.build_digest_sections(
        new, seen, db.counts(), stale=stale,
        deliver_limit=notify.DISCORD_LIMIT,
        wide_net=openjobs.read_status())
    return {"digest": digest, "shown_uids": shown_uids, "active": len(active),
            "hidden": hidden, "verify_hidden": verify_hidden,
            "verdict_failures": failures, "stale": stale}


def cmd_refine(db, args):
    profile = fit.load_profile(args.profile)
    master_path = fit.resolve_config_path("master_resume.yaml", args.master)
    master = yaml.safe_load(master_path.read_text())

    api_key = None
    if not args.no_llm:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("ANTHROPIC_API_KEY not set; skipping LLM verdicts.")

    r = refine_pipeline(db, profile=profile, master=master, top=args.top,
                        no_llm=args.no_llm, max_age=args.max_age,
                        show_all=args.all, api_key=api_key)
    if r["active"] == 0:
        print("No active leads to refine.")
        return
    for f in r["verdict_failures"]:
        print(f"  verdict failed for {f}")
    print(r["digest"])
    if r["hidden"]:
        # Not "stale": on this branch that word means an idle COMMITTED lead,
        # and _fresh_filter exempts every committed state, so this count can
        # only ever hold discoveries and applied leads.
        print(f"\n({r['hidden']} lead(s) hidden by posting age, older than "
              f"{args.max_age/24:.0f}d; --all to include)")
    if r["verify_hidden"]:
        print(f"({r['verify_hidden']} onsite-metro lead(s) held for location check; --all to include)")

    # Push to Discord when asked and a webhook is configured.
    if args.digest:
        cfg = load_cfg(args.config) if args.config else {}
        hook = os.environ.get("DISCORD_WEBHOOK_URL") or cfg.get("discord_webhook", "")
        if notify.post_discord(hook, r["digest"]):
            db.mark_digested(r["shown_uids"])
            print("\n(posted digest to Discord)")
        else:
            print("\n(no Discord webhook configured; digest not pushed)")


def build_parser():
    p = argparse.ArgumentParser(prog="job", description="Job application pipeline.")
    p.add_argument("--db", default=None, help="Path to SQLite db")
    p.add_argument("-c", "--config", default=str(fit.resolve_config_path("companies.yaml")),
                   help="Scan config (default ./companies.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", help="Run a discovery scan and ingest new jobs")

    sp = sub.add_parser("openjobs", help="Wide-net discovery over the open-jobs "
                                         "corpus (no LLM, no API spend)")
    sp.add_argument("--top", type=int, default=None,
                    help="Max new leads to ingest this run (default 15)")
    sp.add_argument("--groups", type=int, default=None,
                    help="How many nearest corpus groups to pull (default 12)")

    sp = sub.add_parser("list", help="List jobs")
    sp.add_argument("--state", choices=[
        "discovered", "queued", "drafted", "ready",
        "applied", "interviewing", "closed", "skipped"])
    sp.add_argument("--limit", type=int)
    sp.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_HOURS,
                    dest="max_age", help="Max posting age in hours (default 720 = 30 days)")
    sp.add_argument("--all", action="store_true",
                    help="Show all ages, ignore the freshness filter")

    sp = sub.add_parser("prune", help="Check postings for liveness "
                        "(dry run unless --apply)")
    sp.add_argument("--state", default="discovered", choices=[
        "discovered", "queued", "drafted", "ready",
        "applied", "interviewing", "closed", "skipped"])
    sp.add_argument("--limit", type=int, help="Check at most N postings")
    sp.add_argument("--apply", action="store_true",
                    help="Mark closed postings skipped (unknowns are never touched)")

    sp = sub.add_parser("show", help="Show one job in detail")
    sp.add_argument("ident")

    for verb, helptext in [("queue", "Mark a job to pursue"),
                           ("ready", "Mark package ready to submit"),
                           ("apply", "Mark as applied (date stamped)")]:
        sp = sub.add_parser(verb, help=helptext)
        sp.add_argument("ident")
        sp.add_argument("--note")

    sp = sub.add_parser("skip", help="Drop a job (optionally with a reason)")
    sp.add_argument("ident")
    sp.add_argument("--note")
    sp.add_argument("--reason", help="Why skipped (feeds fit ranking)")

    sp = sub.add_parser("state", help="Set state directly (validated)")
    sp.add_argument("ident")
    sp.add_argument("state", choices=[
        "discovered", "queued", "drafted", "ready",
        "applied", "interviewing", "closed", "skipped"])
    sp.add_argument("--note")

    sp = sub.add_parser("close", help="Close a job with an outcome")
    sp.add_argument("ident")
    sp.add_argument("--outcome", choices=OUTCOMES, required=True)
    sp.add_argument("--note")
    sp.add_argument("--reason", help="Why closed (feeds fit ranking)")

    sp = sub.add_parser("draft", help="Generate tailored package (resume + cover letter)")
    sp.add_argument("ident")
    sp.add_argument("--master", default=None, help="Path to master_resume.yaml")

    sub.add_parser("next", help="Show the next job to apply to")
    sub.add_parser("stats", help="Pipeline counts")

    sp = sub.add_parser("fetch", help="Ingest one posting by URL "
                        "(LinkedIn, Greenhouse, Lever, Ashby, SmartRecruiters, "
                        "or any page with JobPosting JSON-LD)")
    sp.add_argument("url")

    sp = sub.add_parser("gate", help="Run the fit gate against a posting")
    sp.add_argument("ident")

    sp = sub.add_parser("gate-rule", help="Rule on an UNSURE requirement")
    sp.add_argument("ident")
    sp.add_argument("n", type=int, help="requirement number from the report")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--hard", action="store_true")
    g.add_argument("--soft", action="store_true")
    sp.add_argument("--note", required=True,
                    help="mandatory: why you ruled this way")

    sp = sub.add_parser("gate-override", help="Bypass the gate (reason required)")
    sp.add_argument("ident")
    sp.add_argument("--reason", required=True)

    sp = sub.add_parser("gaps", help="Open gaps, all or for one job")
    sp.add_argument("ident", nargs="?", default=None)

    sp = sub.add_parser("gap-plan", help="Give a gap a plan, hours, and a deadline")
    sp.add_argument("gap_id", type=int)
    sp.add_argument("--plan", required=True)
    sp.add_argument("--hours", type=int, required=True)
    sp.add_argument("--deadline", required=True, help="YYYY-MM-DD")

    sp = sub.add_parser("gap-close", help="Close a gap (reason required)")
    sp.add_argument("gap_id", type=int)
    sp.add_argument("--reason", required=True)

    sp = sub.add_parser("rounds", help="Show or set a job's interview rounds")
    sp.add_argument("ident")
    sp.add_argument("labels", nargs="?", default=None,
                    help="comma-separated round labels, in order")
    sp.add_argument("--add", help="append one round to the existing list")

    sp = sub.add_parser("stage", help="Move the interview marker")
    sp.add_argument("ident")
    sp.add_argument("round",
                    help="round number (1-based) or 'decision'")
    sp.add_argument("--next", help="what is actually next (clears if omitted)")
    sp.add_argument("--on", metavar="YYYY-MM-DD",
                    help="the date the round actually happened (default today). "
                         "The Loop measures silence from this, so recording a "
                         "round late without it resets the clock to today.")

    sp = sub.add_parser("board", help="Render live interview loops to a web page")
    sp.add_argument("--out", default=None,
                    help="output path (default $JOB_APPS_DIR/interviews.html)")
    sp.add_argument("--open", action="store_true", help="open it when written")

    sp = sub.add_parser("refine", help="Score and rank leads; optional Discord digest")
    sp.add_argument("--top", type=int, default=DEFAULT_LLM_TOP,
                    help="Max LLM verdicts to run this pass (default 3)")
    sp.add_argument("--no-llm", action="store_true", dest="no_llm",
                    help="Deterministic scoring only, no API calls")
    sp.add_argument("--digest", action="store_true",
                    help="Push the ranked digest to Discord")
    sp.add_argument("--profile", default=None, help="Path to profile.yaml")
    sp.add_argument("--master", default=None, help="Path to master_resume.yaml")
    sp.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_HOURS,
                    dest="max_age",
                    help="Max posting age in hours for the digest (default 720 = 30 days)")
    sp.add_argument("--all", action="store_true",
                    help="Include stale leads in the digest, ignore the freshness filter")
    return p


DISPATCH = {
    "scan": cmd_scan, "openjobs": cmd_openjobs, "list": cmd_list, "show": cmd_show,
    "queue": cmd_queue, "skip": cmd_skip, "ready": cmd_ready,
    "apply": cmd_apply, "state": cmd_state, "close": cmd_close,
    "draft": cmd_draft, "next": cmd_next, "stats": cmd_stats,
    "fetch": cmd_fetch, "refine": cmd_refine, "prune": cmd_prune,
    "gate": cmd_gate, "gate-rule": cmd_gate_rule,
    "gate-override": cmd_gate_override, "gaps": cmd_gaps,
    "gap-plan": cmd_gap_plan, "gap-close": cmd_gap_close,
    "rounds": cmd_rounds, "stage": cmd_stage, "board": cmd_board,
}


def main():
    args = build_parser().parse_args()
    try:
        db_path = resolve_db_path(args.db)
    except jobdb.DBPathError as e:
        sys.exit(str(e))
    # Surface the active DB on mutating commands so a wrong path is never silent.
    if args.cmd in ("scan", "refine", "draft", "fetch", "prune"):
        print(f"db: {db_path}", file=sys.stderr)
    db = JobDB(db_path)
    try:
        DISPATCH[args.cmd](db, args)
    finally:
        db.close()


if __name__ == "__main__":
    main()
