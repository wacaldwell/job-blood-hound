#!/usr/bin/env python3
"""
fit.py - lead fit-ranking.

Two tiers, mirroring the side-effect-free convention of run_scan/generate:

  score()   - deterministic, free, no network. Ranks every lead from title,
              location, salary, and noise markers. Runs on every refine.
  verdict()  - LLM tier (added in a later task). Reads the full JD and the
              candidate's decision history, returns a fit verdict. Top-N only.

score() takes a plain dict (callers pass dict(row)) so it stays trivially
testable. No em dashes in any emitted string.
"""

import json
import os
import re
from pathlib import Path

import yaml

import freshness as _fr
import gate
import llm

HERE = Path(__file__).resolve().parent


# The operator data files are gitignored, because they hold one person's career
# and targets; the repo tracks `<name>.example.<ext>` templates to copy. An env
# override is honoured first so the file can live outside the checkout, which is
# also how the test suite points itself at the templates.
#
# There is deliberately NO fallback to the .example template when the real file
# is missing. Grading a posting against a fictional resume, or drafting from
# one, is worse than failing: the capability ledger the Fit Gate depends on
# would be somebody else's. Missing config must raise.
CONFIG_ENV = {
    "profile.yaml": "JOB_PROFILE",
    "master_resume.yaml": "JOB_MASTER",
    "companies.yaml": "JOB_CONFIG",
    "ideal-jd.md": "JOB_IDEAL_JD",
}


def resolve_config_path(name, explicit=None):
    """Where an operator data file lives: explicit path, then env, then beside the code."""
    if explicit:
        return Path(explicit)
    env = CONFIG_ENV.get(name)
    if env and os.environ.get(env):
        return Path(os.environ[env])
    return HERE / name


def load_profile(path=None):
    return yaml.safe_load(resolve_config_path("profile.yaml", path).read_text())


def load_master(path=None):
    """Parse master_resume.yaml. Callers pass the result to gate.load_profile()
    to get the do_not_claim ledger that score() can demote against."""
    return yaml.safe_load(resolve_config_path("master_resume.yaml", path).read_text())


# The ceiling a ledger-tripping lead is held under. Chosen to sit below score()'s
# base of 30, so such a lead lands beneath even a title-less, location-less one:
# a role built on a competency the operator can never claim is worth less attention than
# an unremarkable role that is merely a poor match. It is a CEILING, never a
# floor, so a lead already scoring lower is not raised to it.
LEDGER_CAP = 25


def _haystack(job):
    return " ".join([
        (job.get("title") or ""),
        (job.get("location") or ""),
        (job.get("description") or ""),
    ]).lower()


def _title_matches(title, terms):
    """True if `title` matches any term, by substring OR by word subset.

    Both, because either alone loses real matches.

    Substring catches stem and plural variants: "Manager, Solutions Architects"
    matches "solutions architect" only this way, since "architect" is not a word
    in "Architects".

    Word-subset catches reordering, which substring cannot. "Manager, DevOps
    Engineering" is an engineering manager job but does not contain the string
    "engineering manager". That miss scored a role the operator reached a third
    interview round on at 50, its title contributing nothing at all.
    """
    t = (title or "").lower()
    tw = set(re.findall(r"[a-z0-9]+", t))
    return any(term in t or set(re.findall(r"[a-z0-9]+", term)) <= tw
               for term in terms)


def score(job, profile, do_not_claim=None):
    """Return (int 0..100, reasons string). Deterministic, no network.

    `do_not_claim` is optional: omit it and scoring behaves exactly as it did
    before the ledger pre-filter existed.
    """
    w = profile["weights"]
    title = (job.get("title") or "").lower()
    location = (job.get("location") or "").lower()
    hay = _haystack(job)
    reasons = []
    total = 30  # base

    # Customer-facing vendor roles, in two flavours: presales (demos, PoVs,
    # quota) and professional services (implementations, engagements). Both
    # share the words "Solutions Architect" with the AWS cloud role the operator wants
    # and are otherwise a different job. Detected BEFORE the title bonus so it
    # can be withheld: at +40 it is the largest weight here and cannot be
    # reliably out-penalized.
    sales_hits = [m for m in profile.get("sales_markers", []) if m in hay]
    services_hits = [m for m in profile.get("services_markers", []) if m in hay]

    tt = profile["target_titles"]
    if not (sales_hits or services_hits):
        if _title_matches(title, tt["strong"]):
            total += w["title_strong"]; reasons.append("title:strong")
        elif _title_matches(title, tt["good"]):
            total += w["title_good"]; reasons.append("title:good")
    if sales_hits:
        total += w["sales_penalty"] * len(sales_hits)
        reasons.append(f"sales-role:{len(sales_hits)}")
    if services_hits:
        total += w["services_penalty"] * len(services_hits)
        reasons.append(f"services-role:{len(services_hits)}")

    if any(t in location for t in profile["remote_ok"]):
        total += w["remote"]; reasons.append("remote")
    elif any(t in location for t in profile["onsite_ok"]):
        total += w["onsite"]; reasons.append("onsite-NC")
    else:
        total += w["no_location_match"]; reasons.append("location:no-match")

    sal = job.get("salary_min")
    if sal:
        if sal >= profile["salary_floor"]:
            total += w["salary_meets"]; reasons.append("salary:meets")
        else:
            total += w["salary_below"]; reasons.append("salary:below")

    coding_hits = [m for m in profile["coding_bar_markers"] if m in hay]
    if coding_hits:
        total += w["coding_penalty"] * len(coding_hits)
        reasons.append(f"coding-bar:{len(coding_hits)}")

    if any(m in hay for m in profile["exclude_markers"]):
        total += w["exclude_penalty"]; reasons.append("exclude-marker")

    total = max(0, min(100, total))

    # The Fit Gate's do_not_claim ledger, applied as a RANKING signal. This is
    # not a second gate: it blocks nothing, decides nothing, and only ever moves
    # a score down. gate.require_pass() remains the only thing that can stop an
    # artifact. Without this, a lead whose central requirement is a forbidden
    # competency still scored on title and location and rode to the top of the
    # digest, and the gate only refused it after the attention was spent
    # (a live lead, 2026-08-20: scored 90, gated DO_NOT_APPLY on the same JD).
    #
    # It can only demote what it can SEE. A lead with no stored description is
    # matched on title alone and is usually left untouched, which is the
    # fail-safe direction: unseen is not the same as clean, and burying a lead
    # nobody has read yet would hide real jobs. The gate still reads the full JD.
    if do_not_claim:
        hit = gate.ledger_hit(_haystack(job), do_not_claim)
        if hit:
            total = min(total, LEDGER_CAP)
            reasons.append(f"ledger:{hit.get('claim', '?')}")

    return int(total), "; ".join(reasons)


# States that count as a positive ("pursue") signal.
_PURSUED_STATES = ("queued", "drafted", "ready", "applied", "interviewing")
# Outcomes on a closed job that count as negative.
_REJECTED_OUTCOMES = ("rejected", "withdrawn", "ghosted")
# Outcomes that mean the job was actually won. Not overridable by a down-vote.
_WON_OUTCOMES = ("offer", "accepted")


def build_history(db, limit=20):
    """Build the few-shot decision corpus from the DB, newest decision first.

    Pursued: jobs the operator moved into the work pipeline (or closed favorably).
    Rejected: jobs skipped, or closed with a rejecting outcome.
    Untriaged 'discovered' jobs carry no decision and are excluded, unless the operator voted on them, which counts as liked/disliked.
    """
    rows = db.conn.execute(
        "SELECT * FROM jobs "
        "WHERE state IN ('queued','drafted','ready','applied','interviewing',"
        "'skipped','closed') "
        "   OR (state = 'discovered' AND vote IS NOT NULL) "
        "ORDER BY updated_at DESC"
    ).fetchall()

    out = []
    for r in rows:
        state = r["state"]
        # Read the lifecycle first, then let a down-vote veto it below. Doing it
        # in that order rather than special-casing states is what keeps 'closed'
        # covered: a records-only application that later closes with any
        # non-rejecting outcome used to flip back to "pursued".
        #
        # Every reason source read below (notes, skip_reason, close_reason,
        # vote_note) is raw here. _history_block normalizes whitespace and
        # length uniformly for all of them at render time; see _flatten there.
        if state in _PURSUED_STATES:
            decision, reason = "pursued", (r["notes"] or "")
        elif state == "skipped":
            decision, reason = "rejected", (r["skip_reason"] or "")
        elif state == "closed":
            if (r["outcome"] or "") in _REJECTED_OUTCOMES:
                decision = "rejected"
            else:
                decision = "pursued"
            reason = r["close_reason"] or ""
        elif state == "discovered":
            # Voted but untriaged: a softer signal than a lifecycle decision.
            decision = "liked" if r["vote"] == "up" else "disliked"
            reason = r["vote_note"] or ""
        else:
            continue

        # A down-vote vetoes any PURSUED reading. The operator files some
        # applications purely for unemployment work-search records, on roles they
        # do not rate (one such, 2026-07-24); read from state alone those look exactly
        # like real pursuit, so each one would teach the scorer to surface more
        # like it. Rendered as a full rejection, not the weaker "disliked",
        # because it has to outweigh an 'applied' row.
        #
        # One-directional, like the gate: a vote can only make the signal more
        # negative. An up-vote never rescues a skipped job (un-skipping is how
        # you do that), and a down-vote on an untriaged 'discovered' lead stays
        # the softer "disliked" it already was.
        #
        # The one exception is a job actually won. An offer is the strongest
        # positive signal in the corpus and is a fresher fact than a vote cast
        # before the process ran, so it is not overridable.
        if (decision == "pursued" and r["vote"] == "down"
                and (r["outcome"] or "") not in _WON_OUTCOMES):
            decision = "rejected"
            reason = r["vote_note"] or reason
        out.append({"title": r["title"], "company": r["company"],
                    "decision": decision, "reason": reason})
        if len(out) >= limit:
            break
    return out


VERDICT_SYSTEM = """You score how well a job fits one candidate, learning from
the candidate's own past decisions. You are a triage judge, not a writer.

You are given the candidate's master resume, the full job description, and a
list of roles the candidate previously chose to PURSUE or REJECT (with reasons),
plus lighter LIKED or DISLIKED signals from quick votes on untriaged leads.
Lifecycle decisions (PURSUE, REJECT) are stronger evidence than votes.
Score this role the way the candidate would, paying attention to the real
hands-on-coding bar: this candidate favors solutions-architect, platform, and
leadership roles and avoids heavy individual-contributor coding jobs.

Judge only from the resume and JD. Do not invent candidate experience.

Hard voice rule: never use em dashes or double hyphens. Use commas or separate
sentences.

Return ONLY valid JSON, no markdown fences, with this exact shape:
{
  "llm_fit_score": <integer 0-100>,
  "llm_rationale": "one sentence on why it fits or does not",
  "llm_coding_bar": "light | medium | heavy, plus a few words"
}"""


def _default_fetch(job):
    import job_generate
    return job_generate.fetch_description(job)


def _call_anthropic(system, user, api_key):
    prov = llm.resolve_provider(component="fit")
    if api_key and prov.name == llm.DEFAULT_PROVIDER:
        prov = prov._replace(api_key=api_key)
    return llm.call_messages(system, user, max_tokens=1000, provider=prov,
                             component="fit")


def _flatten(text, limit=280):
    """Collapse any run of whitespace (including newlines) to single spaces
    and cap the result at limit. The corpus format is one bullet per line, so
    an embedded newline in a rendered field would fabricate a fake entry that
    the model reads as a real past decision. The default of 280 is the size
    this format was built around, and matches the write API's vote_note cap.
    """
    return " ".join((text or "").split())[:limit]


def _history_block(history):
    if not history:
        return "(no past decisions yet)"
    lines = []
    for h in history:
        # title and company come from ATS APIs, not the operator, so they are
        # lower risk than reason, but they sit on the same line and a rogue
        # feed could still embed a newline. Cap them short since they are
        # names, not prose.
        title = _flatten(h["title"], limit=120)
        company = _flatten(h["company"], limit=80)
        reason = _flatten(h.get("reason"))
        tail = f" - {reason}" if reason else ""
        lines.append(f"- {h['decision'].upper()}: {title} @ {company}{tail}")
    return "\n".join(lines)


def _parse_verdict(raw):
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?", "", text).rsplit("```", 1)[0].strip()
    data = json.loads(text)
    return {
        "llm_fit_score": int(data["llm_fit_score"]),
        "llm_rationale": data.get("llm_rationale", ""),
        "llm_coding_bar": data.get("llm_coding_bar", ""),
    }


def verdict(job, master, history, api_key, jd_text=None,
            fetch_jd=None, call=None):
    """LLM fit verdict for one job. Top-N use only; one API call per job.

    jd_text/fetch_jd/call are injectable so the function is testable offline.
    """
    if jd_text is None:
        fetch_jd = fetch_jd or _default_fetch
        jd_text = fetch_jd(job)
    call = call or _call_anthropic

    user = f"""CANDIDATE MASTER RESUME:
{json.dumps(master, indent=2)}

PAST DECISIONS (learn the candidate's taste from these):
{_history_block(history)}

ROLE TO SCORE:
Company: {job.get('company')}
Title: {job.get('title')}
Location: {job.get('location')}

JOB DESCRIPTION:
{(jd_text or '')[:12000]}

Return the verdict JSON now."""

    return _parse_verdict(call(VERDICT_SYSTEM, user, api_key))


def ledger_demoted(job):
    """True when the last deterministic score hit the do_not_claim ledger.

    Read off `fit_reasons`, which is the structured reason column score()
    writes and which reloads with the row, so this works on a fresh dict and on
    a row the digest reloaded alike. Token-exact rather than a substring test,
    so an unrelated reason that merely contains the word does not demote a lead.

    It reports on the LAST DETERMINISTIC SCORE, not on the ledger itself, so a
    row nothing has scored yet reads as clean. job_ingest writes llm_fit_score
    and never fit_score/fit_reasons, so a freshly ingested lead is exactly that
    row until something rescores it. refine_pipeline scores every active lead
    before it ranks anything, so the digest is right; `bin/jh list` and the MCP
    job_list read stored columns straight and can show an un-demoted ingest for
    the window between that ingest and the next refine. Sharpening this into a
    live ledger match would mean loading master_resume.yaml on every list call,
    which is the trade being declined, not an oversight.
    """
    reasons = job.get("fit_reasons") or ""
    return any(t.strip().startswith("ledger:") for t in reasons.split(";"))


def rank_key(job):
    """The score to DISPLAY for a job: the LLM verdict if present, else the
    deterministic score. This is the number shown, not the sort order.

    A ledger hit caps the verdict too. score() caps `fit_score`, but a lead
    that already carries an `llm_fit_score` (from an earlier refine, or carried
    over from a web-inbox ingest) kept displaying it, and refine
    deliberately
    does not recompute a verdict a lead already has, so a forbidden lead scored
    95 stayed at 95 forever on a deterministic score of 25. The verdict was
    formed WITHOUT the ledger, so for these leads it is not the trusted signal
    it is everywhere else. Capping here rather than overwriting the column
    keeps the record of what the model actually said, and keeps
    `llm_fit_score is not None` meaning "already vetted", which is what stops
    refine from re-spending the API call on the lead every single run.
    """
    v = job.get("llm_fit_score")
    score = v if v is not None else (job.get("fit_score") or 0)
    if ledger_demoted(job):
        # A ceiling, never a floor, exactly as in score().
        score = min(score, LEDGER_CAP)
    return score


def _age_penalty(job):
    """Points shaved off a lead's rank for staleness: ~1 point per week of age,
    capped, so a fresh strong role beats a stale stronger one. An old listing is
    usually filled. Undated postings take a small fixed penalty so an unknown
    age does not float to the top."""
    h = _fr.age_hours(job.get("posted_at"))
    if h is None:
        return 5.0
    return min(20.0, (h / 24.0) / 7.0)


def sort_key(job):
    """The order to RANK a job in. Vetted leads (those with an LLM verdict)
    sort above un-vetted ones, because the deterministic score saturates and a
    verdict is the trusted signal. Within each tier, the displayed score minus a
    freshness penalty wins, so fresh-and-strong rises above stale-and-stronger.
    Use with reverse=True.

    A ledger hit forfeits the vetted tier as well as the score. The tier exists
    because a verdict is the trusted signal; a verdict formed without the
    ledger is not one, and leaving the row in the tier would float a forbidden
    lead above every clean unvetted one no matter how far its score was capped.
    """
    has_verdict = 1 if job.get("llm_fit_score") is not None else 0
    if ledger_demoted(job):
        has_verdict = 0
    return (has_verdict, rank_key(job) - _age_penalty(job))


def _no_dash(s):
    return (s or "").replace("—", ", ").replace("--", ", ")


_LOC_ABBR = {"remote": "rem", "onsite/hybrid": "onsite"}


def _short_age(posted_at, date_source):
    """Compact age token for the digest: '6h', '32d', '~19d' (approx), '?'."""
    h = _fr.age_hours(posted_at)
    if h is None:
        return "?"
    s = f"{int(round(h))}h" if h < 48 else f"{int(round(h / 24))}d"
    return ("~" + s) if _fr.is_approximate(date_source) else s


def _digest_line(j):
    """One digest line: `fit` . age . loc . **Company**: Title . [open](url).

    Display text is sanitized via _no_dash; the URL is NOT (Workday URLs
    contain '--' that _no_dash would corrupt)."""
    score = rank_key(j)
    age = _short_age(j.get("posted_at"), j.get("date_source"))
    loc = _LOC_ABBR.get(j.get("location_type") or "", "")
    meta = f"`{score:>2}` · {age:>4}"
    if loc:
        meta += f" · {loc}"
    line = f"{meta} · **{_no_dash(j.get('company', ''))}**: {_no_dash(j.get('title', ''))}"
    if j.get("url"):
        line += f" · [open]({j['url']})"
    return line


def build_digest(ranked, counts, limit=10):
    """Compact, scannable Discord digest: one line per lead, vetted first, then
    an un-vetted 'pending verdict' tier. Each line is
    `fit` . age . loc . **Company**: Title . [open](url)
    so the list reads like a table and stays mobile-friendly. Sorted by
    sort_key."""
    ordered = sorted(ranked, key=sort_key, reverse=True)[:limit]
    lines = ["**Job-hound digest**  (fit · age · loc · role)", ""]
    pending_header_done = False
    for j in ordered:
        # Label the boundary where the un-vetted tier begins.
        if j.get("llm_fit_score") is None and not pending_header_done:
            lines.append("**Pending verdict (not yet scored):**")
            pending_header_done = True
        lines.append(_digest_line(j))
    if counts:
        lines.append("")
        lines.append("Pipeline: " + " · ".join(f"{k} {v}" for k, v in counts.items()))
    # Display text was already sanitized per line; do NOT re-run _no_dash over
    # the whole digest, as that would corrupt "--" inside job URLs.
    return "\n".join(lines)


def _seen_oneliner(j):
    """Collapsed recap token for an already-sent lead: 'Company 88'."""
    return f"{_no_dash(j.get('company', ''))} {rank_key(j)}"


def _stale_digest_line(j):
    """One Needs attention line: `fit` . idle . **Company**: Title . [open].

    Mirrors _digest_line but swaps posting age for idle time, which is the
    only number that matters once a lead is already committed to."""
    score = rank_key(j)
    idle = j.get("idle_label", "")
    line = f"`{score:>2}` · {idle} · **{_no_dash(j.get('company', ''))}**: {_no_dash(j.get('title', ''))}"
    if j.get("url"):
        line += f" · [open]({j['url']})"
    return line


def _delivered_uids(lines, uid_lines, limit):
    """The uids whose line survives a hard truncation of the joined text at
    `limit` characters, or every announced uid when `limit` is None.

    notify.post_discord cuts the body at DISCORD_LIMIT with no warning and no
    error, so a line past that cut was never actually read. Stamping it
    digested anyway would spend a lead's one-time 'New' announcement on a
    message nobody saw and demote it to the collapsed recap forever. A
    partially delivered line counts as lost: half a URL is not a lead.

    Offsets are monotonic, so the first line that does not fit ends it.
    """
    out = []
    pos = 0
    for i, line in enumerate(lines):
        end = pos + len(line)
        if limit is not None and end > limit:
            break
        out.extend(uid_lines.get(i, ()))
        pos = end + 1  # "\n".join adds one separator per line
    return out


def wide_net_line(status):
    """One line reporting the wide net's last run, or "" when it has none.

    Always says something when the stage reported, in both directions. A line
    only on failure would leave "working" and "never deployed" looking
    identical, which is the failure this exists to prevent.
    """
    if not status:
        return ""
    err = status.get("error")
    if err:
        # Trim to the useful part: the reason, not the traceback shape.
        reason = str(err).split("\n")[0][:120]
        return f"Wide net: unavailable ({reason})"
    added, found = status.get("added", 0), status.get("found", 0)
    return f"Wide net: {added} new from {found} candidates"


def build_digest_sections(new, seen, counts, new_limit=12, seen_limit=10,
                          stale=None, stale_limit=10, deliver_limit=None,
                          wide_net=None):
    """Digest with up to three sections: leads that need action, then leads
    never sent, then a compact recap of still-open leads already sent.

    `stale` is a list of committed leads that have sat untouched (see
    staleness.py), each carrying an `idle_label` key. It leads the digest
    because it is the only part about the operator rather than about the market, and
    it is omitted entirely when empty so a clean pipeline stays quiet. It is
    capped at `stale_limit` with a `(+N more)` tail, the same shape the seen
    recap uses: an unbounded section grows until it pushes the sections below
    it past the delivery cut.

    `deliver_limit` is the character budget the transport will actually
    deliver (the caller passes notify.DISCORD_LIMIT). shown_uids is filtered
    to the lines that fit inside it, so a lead whose line was truncated away
    is never stamped as digested. None means no truncation.

    Stale leads are deliberately NOT in the returned shown_uids: that list
    drives mark_digested, and a nag that stops after one appearance is not a
    nag. They keep appearing until the operator acts on them.

    Both `new` and `seen` are ranked here by sort_key. `stale` is ranked by
    rank_key instead: sort_key subtracts a POSTING-age penalty, and this is
    the one section whose whole subject is IDLE time, so mixing the two
    clocks could sort a 60-day-idle lead below an 8-day-idle one. rank_key is
    also the number _stale_digest_line displays, so the order matches what is
    printed. Returns (text, shown_uids).
    """
    new_top = sorted(new, key=sort_key, reverse=True)[:new_limit]
    seen_sorted = sorted(seen, key=sort_key, reverse=True)
    seen_shown = seen_sorted[:seen_limit]

    lines = ["**Job-hound digest**  (fit · age · loc · role)", ""]
    uid_lines = {}  # index into `lines` -> the uids that line announces

    if stale:
        stale_sorted = sorted(stale, key=rank_key, reverse=True)
        stale_shown = stale_sorted[:stale_limit]
        lines.append(f"**Needs attention** ({len(stale_sorted)})")
        for j in stale_shown:
            lines.append(_stale_digest_line(j))
        extra = len(stale_sorted) - len(stale_shown)
        if extra > 0:
            lines.append(f"(+{extra} more)")
        lines.append("")

    if new_top:
        lines.append(f"**New since last digest** ({len(new_top)})")
        for j in new_top:
            uid_lines[len(lines)] = [j["uid"]]
            lines.append(_digest_line(j))
    else:
        lines.append("No new leads today.")

    if seen_sorted:
        lines.append("")
        lines.append(f"**Still open** ({len(seen_sorted)} previously sent)")
        recap = " · ".join(_seen_oneliner(j) for j in seen_shown)
        extra = len(seen_sorted) - len(seen_shown)
        if extra > 0:
            recap += f" · (+{extra} more)"
        uid_lines[len(lines)] = [j["uid"] for j in seen_shown]
        lines.append(recap)

    if counts:
        lines.append("")
        lines.append("Pipeline: " + " · ".join(f"{k} {v}" for k, v in counts.items()))

    # Second discovery source's health, beside the pipeline counts. Inside the
    # delivery budget rather than appended by the caller, so a long digest
    # truncates this too instead of overrunning the transport limit.
    health = wide_net_line(wide_net)
    if health:
        if not counts:
            lines.append("")
        lines.append(health)

    return "\n".join(lines), _delivered_uids(lines, uid_lines, deliver_limit)
