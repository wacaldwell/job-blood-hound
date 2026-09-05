#!/usr/bin/env python3
"""
backfill.py - repair stored descriptions and deterministic scores in place.

Why this exists: fit_score and fit_reasons are STORED columns, so a scoring fix
only ever applies to rows scored after it ships. The daily refine pass rescores
active leads, but it never backfills descriptions and it skips 'closed' and
'skipped' rows entirely, so a lead wrongly skipped by a scoring bug can never
climb back out on its own.

Two passes, both idempotent:

  descriptions - rows with an empty description get one refetched. Greenhouse is
                 fetched in BULK, one request per company board via content=true,
                 rather than one per posting. Everything else falls back to
                 job_generate.fetch_description, one request per row. Every
                 request is paced by job_monitor.SLEEP_BETWEEN_CALLS, including
                 after a failure. Unreachable rows are counted and skipped,
                 never blanked.
  scores       - fit_score and fit_reasons recomputed for EVERY row from the
                 current profile. Free, no network.

Writes exactly three columns: description, fit_score, fit_reasons. It does not
use jobdb.set_fields, because that bumps updated_at, and build_history orders by
updated_at DESC and takes the newest 20. Bumping 400 rows would flood the
model's decision corpus with whatever this script happened to touch last.
updated_at is preserved explicitly.

Dry run by default: no JOB DATA is written without --apply.

To be precise about what a dry run still does, opening the database applies the
project's standard additive schema migration, because JobDB does that in its
constructor. Every job-hound command behaves this way, including read-only ones
like `list` and `stats`, and CLAUDE.md documents it as the deploy mechanism. It
is idempotent (a second open leaves the file byte-identical) and touches no job
rows, but it does mean a dry run opens the file read-write.

    python backfill.py                  # show what would change
    python backfill.py --apply          # write it
    python backfill.py --scores-only    # skip the network pass
"""

import argparse
import os
import sys
import time
from collections import defaultdict

import fit
import gate
import job_generate
import job_monitor
import jobdb

def _greenhouse_bulk(company):
    """{ext_id: description} for one Greenhouse board, in a single request."""
    return {str(j["id"]): j.get("description", "")
            for j in job_monitor.fetch_greenhouse(company)}


def fetch_missing(db, rows, verbose=True):
    """Return {uid: description} for rows whose description is empty.

    Greenhouse boards are fetched once per company. Everything else is fetched
    per row. Failures are reported and omitted, never written as an empty
    string: a blank description is what caused the original problem.
    """
    missing = [r for r in rows if not (r["description"] or "").strip()]
    found, failed = {}, []

    by_company = defaultdict(list)
    for r in missing:
        if r["ats"] == "greenhouse":
            by_company[r["company"]].append(r)

    # The sleeps sit in `finally` on purpose. A board that errors is exactly when
    # skipping the delay would turn this into a tight loop against someone's
    # public endpoint, which is the thing CLAUDE.md forbids.
    for company, group in sorted(by_company.items()):
        try:
            board = _greenhouse_bulk(company)
        except Exception as e:
            failed.extend((r["uid"], f"board {company}: {e}") for r in group)
        else:
            for r in group:
                desc = board.get(str(r["ext_id"]), "")
                if desc.strip():
                    found[r["uid"]] = desc
                else:
                    failed.append((r["uid"],
                                   "not on the board (pulled or expired)"))
        finally:
            time.sleep(job_monitor.SLEEP_BETWEEN_CALLS)

    for r in missing:
        if r["ats"] == "greenhouse":
            continue
        try:
            desc = job_generate.fetch_description(r)
        except Exception as e:
            failed.append((r["uid"], str(e)[:80]))
        else:
            if (desc or "").strip():
                found[r["uid"]] = desc
            else:
                failed.append((r["uid"], "empty description returned"))
        finally:
            time.sleep(job_monitor.SLEEP_BETWEEN_CALLS)

    if verbose:
        print(f"  descriptions: {len(missing)} missing, "
              f"{len(found)} fetched, {len(failed)} unreachable")
    return found, failed


def plan(db, profile, descriptions, do_not_claim=None):
    """Compute every change without writing. Returns a list of change dicts.

    `do_not_claim` is the gate's ledger, passed through to fit.score so the
    repaired rows carry the same `ledger:` token a refine would write. It is
    not optional in spirit: fit_reasons is what fit.rank_key and fit.sort_key
    read to hold a forbidden lead under LEDGER_CAP, and this pass rewrites that
    column for EVERY row, closed and skipped included. Scoring without it here
    silently promoted every ledger-tripping lead back above the clean ones
    until the next refine happened to rescore it.
    """
    changes = []
    for r in db.list():
        row = dict(r)
        new_desc = descriptions.get(row["uid"])
        if new_desc:
            row["description"] = new_desc
        score, reasons = fit.score(row, profile, do_not_claim=do_not_claim)
        if new_desc or score != row["fit_score"] or reasons != row["fit_reasons"]:
            changes.append({
                "uid": row["uid"], "slug": row["slug"], "title": row["title"],
                "state": row["state"],
                "old_score": row["fit_score"], "new_score": score,
                "old_reasons": row["fit_reasons"], "new_reasons": reasons,
                "description": new_desc,
            })
    return changes


def apply(db, changes):
    """Write description, fit_score and fit_reasons. Preserves updated_at."""
    for c in changes:
        if c["description"] is not None:
            db.conn.execute(
                "UPDATE jobs SET description = ?, fit_score = ?, fit_reasons = ? "
                "WHERE uid = ?",
                (c["description"], c["new_score"], c["new_reasons"], c["uid"]))
        else:
            db.conn.execute(
                "UPDATE jobs SET fit_score = ?, fit_reasons = ? WHERE uid = ?",
                (c["new_score"], c["new_reasons"], c["uid"]))
    db.conn.commit()
    return len(changes)


def _report(changes, limit=40):
    scored = [c for c in changes if c["old_score"] != c["new_score"]]
    up = [c for c in scored if (c["old_score"] or 0) < c["new_score"]]
    down = [c for c in scored if (c["old_score"] or 0) > c["new_score"]]
    print(f"\n{len(changes)} row(s) change; {len(scored)} change SCORE "
          f"({len(up)} up, {len(down)} down)\n")
    for label, group in (("DOWN", down), ("UP", up)):
        if not group:
            continue
        print(f"--- {label} ({len(group)}) ---")
        ordered = sorted(group, key=lambda c: abs((c["old_score"] or 0) - c["new_score"]),
                         reverse=True)
        for c in ordered[:limit]:
            old = c["old_score"] if c["old_score"] is not None else "-"
            print(f"  {str(old):>3} -> {c['new_score']:3}  [{c['state'][:12]:12}] "
                  f"{c['title'][:52]:52} {c['new_reasons']}")
        if len(ordered) > limit:
            print(f"  ... and {len(ordered) - limit} more")
        print()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    ap.add_argument("--scores-only", action="store_true",
                    help="skip the description refetch, rescore only")
    ap.add_argument("--limit", type=int, default=40,
                    help="rows to show per direction in the report")
    args = ap.parse_args(argv)

    try:
        db = jobdb.JobDB(jobdb.resolve_db_path())
    except jobdb.DBPathError as e:
        sys.exit(str(e))
    profile = fit.load_profile()
    # Read from the SAME master the gate reads, and fail SAFE like refine does:
    # a malformed ledger costs the demotion, it does not take down a repair
    # tool whose whole job is fixing damaged rows.
    try:
        _caps, ledger = gate.load_profile(fit.load_master())
    except (gate.ProfileError, OSError) as e:
        print(f"do_not_claim ledger unavailable ({e}); scoring without it.")
        ledger = None
    rows = [dict(r) for r in db.list()]
    print(f"{len(rows)} row(s) in {db.path if hasattr(db, 'path') else 'the database'}")

    descriptions, failed = ({}, [])
    if not args.scores_only:
        descriptions, failed = fetch_missing(db, rows)

    changes = plan(db, profile, descriptions, do_not_claim=ledger)
    _report(changes, args.limit)

    if failed:
        print(f"--- unreachable ({len(failed)}), left untouched ---")
        for uid, why in failed[:10]:
            print(f"  {uid[:52]:52} {why}")
        if len(failed) > 10:
            print(f"  ... and {len(failed) - 10} more")
        print()

    if args.apply:
        n = apply(db, changes)
        print(f"WROTE {n} row(s). description, fit_score, fit_reasons only; "
              f"updated_at preserved.")
    else:
        print("DRY RUN. No job data written. Re-run with --apply.")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
