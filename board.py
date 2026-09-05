#!/usr/bin/env python3
"""
board.py - Renders live interview loops as a self-contained HTML page.

One lane per job in the `interviewing` state, drawn as a transit line: a node
per round in that job's own order, plus a terminal `decision` node, with a
marker at the round currently in play.

Pure rendering. Reads rows, returns a string, writes a file when asked. No
network, no database access, no model call, so it stays cheap enough to
regenerate on every change.

The output has no external references at all (no CDN fonts, no remote images,
no fetch) so the file works from disk, from a phone, and from an rsync copy.
"""

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import jobdb

# One hue per lane, cycled. Chosen to stay legible on both the light and the
# dark ground rather than being inverted for one of them.
ACCENTS = [
    ("#0F8C82", "#4FD1C4"),   # teal
    ("#B4762A", "#E8B368"),   # amber
    ("#7A5AA8", "#B79DE0"),   # violet
    ("#B24A5C", "#E8919E"),   # rose
    ("#4A7FB5", "#84B6E8"),   # blue
    ("#5E8C42", "#98C97A"),   # green
]


def days_since(iso):
    """Whole days between an ISO timestamp and now. None if unparseable."""
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - then
    return max(0, delta.days)


def age_label(iso):
    d = days_since(iso)
    if d is None:
        return ""
    if d == 0:
        return "today"
    return f"{d}d"


def display_company(name):
    """Make an ATS slug presentable without inventing capitalisation.

    Company arrives as whatever the board API called it, often a lowercase
    slug ("acme-robotics"). Separators become spaces, and an all-lowercase
    name gets title case. A name that already carries capitals is left exactly
    as it is, so "GitLab" and "eBay" survive untouched.
    """
    clean = (name or "").replace("-", " ").replace("_", " ").strip()
    return clean.title() if clean and clean == clean.lower() else clean


def lane_model(row):
    """Flatten one job row into what the template needs.

    `stops` is the round list plus a terminal decision node. `marker` is a
    1-based index into that list, or None when nothing has been set yet, which
    renders as a lane with no position rather than being hidden. Hiding it
    would make the board undercount live conversations.
    """
    rounds = jobdb.rounds_of(row)
    if not rounds:
        rounds = list(jobdb.DEFAULT_ROUNDS)
    stops = rounds + ["decision"]

    if jobdb._col(row, "interview_decision"):
        marker = len(stops)
    else:
        at = jobdb._col(row, "interview_at")
        marker = at if isinstance(at, int) and 1 <= at <= len(rounds) else None

    return {
        "company": display_company(row["company"]),
        "title": row["title"],
        "stops": stops,
        "marker": marker,
        "next": jobdb._col(row, "interview_next") or "",
        "age": age_label(jobdb._col(row, "interview_updated")),
        "url": row["url"] or "",
    }


# A seeded placeholder: the bare word, or the word and a number and nothing
# else. It names a slot, never who was in one, so it is never a detail worth
# printing. Matching only the placeholder that happens to equal its own caption
# left the worst case standing: a "round 3" seed that a reorder or a shortened
# list moved into the second slot still printed under the derived "round 1",
# which is the exact contradiction the derived caption exists to remove. Rows
# seeded before the placeholder lost its number still carry "round 1".."round
# 3", so this has to keep matching them; there is no migration.
# Anchored on both ends on purpose, so a real label that merely opens with the
# word ("round table with the platform team") survives.
_PLACEHOLDER = re.compile(r"round(\s+\d+)?\Z", re.IGNORECASE)


def is_placeholder(label):
    """True when a stored round label names a slot rather than who was in it.

    Public because `jh rounds` needs the same answer: it prints the list
    position beside the stored label, and a legacy "round 1" sitting in
    position 2 puts two disagreeing numbers on one line. One predicate, so the
    board and the CLI can never drift on what counts as unfilled.
    """
    return bool(_PLACEHOLDER.match((label or "").strip()))


def captions(stops):
    """Split each stop into its frame caption and its detail.

    The frame is what stays the same across every lane, so it is what the eye
    scans: recruiter, round 1, round 2, round 3, decision. Who was actually in
    a round is the detail, and it rides underneath in small print.

    The caption is derived from position rather than stored, so relabelling a
    round can never put a label naming one number into a slot holding another.
    Round numbers count only the real rounds, skipping the recruiter screen and
    the decision terminal. A detail identical to its caption, and any generic
    "round N" seed nobody has filled in yet, is dropped rather than printed
    under a caption it may well disagree with.
    """
    out, n, last = [], 0, len(stops)
    for i, label in enumerate(stops, start=1):
        if i == last:
            cap = "decision"
        elif label.strip().lower() == "recruiter":
            cap = "recruiter"
        else:
            n += 1
            cap = f"round {n}"
        detail = label.strip()
        if detail.lower() == cap.lower() or is_placeholder(detail):
            detail = ""
        out.append((cap, detail))
    return out


def _lane_html(lane, index):
    e = html.escape
    light, dark = ACCENTS[index % len(ACCENTS)]
    parts = []
    stops = captions(lane["stops"])
    n = len(stops)
    for i, (cap, detail) in enumerate(stops, start=1):
        marker = lane["marker"]
        if marker is None:
            cls = "future"
        elif i < marker:
            cls = "done"
        elif i == marker:
            cls = "here"
        else:
            cls = "future"
        terminal = " terminal" if i == n else ""
        det = f'<span class="det">{e(detail)}</span>' if detail else ""
        parts.append(
            f'<div class="stop {cls}{terminal}">'
            f'<i></i><div class="lbl"><span class="cap">{e(cap)}</span>'
            f'{det}</div></div>'
        )
        if i < n:
            link_done = "done" if (lane["marker"] or 0) > i else ""
            parts.append(f'<div class="link {link_done}"></div>')

    head_role = f'<span class="role">{e(lane["title"])}</span>'
    company = e(lane["company"])
    if lane["url"]:
        company = f'<a href="{e(lane["url"])}">{company}</a>'
    age = f'<span class="age">{e(lane["age"])}</span>' if lane["age"] else ""
    nxt = (f'<p class="next">{e(lane["next"])}</p>'
           if lane["next"] else
           '<p class="next none">no note on what is next</p>')

    return (
        f'<section class="lane" style="--a:{light};--a-dark:{dark}">'
        f'<header><span class="co">{company}</span>{head_role}{age}</header>'
        f'<div class="track-wrap"><div class="track">{"".join(parts)}</div></div>'
        f'{nxt}</section>'
    )


CSS = """
:root {
  --ground:#F7F8FA; --surface:#FFFFFF; --edge:#E2E6EB; --edge-soft:#EDF0F3;
  --ink:#14171B; --muted:#5B636E; --faint:#8B929C;
  --sans:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root{--ground:#101316;--surface:#191D21;--edge:#2A3036;--edge-soft:#212629;
        --ink:#E8EBEE;--muted:#98A0AA;--faint:#6F7781;}
}
:root[data-theme="dark"]{--ground:#101316;--surface:#191D21;--edge:#2A3036;
  --edge-soft:#212629;--ink:#E8EBEE;--muted:#98A0AA;--faint:#6F7781;}
:root[data-theme="light"]{--ground:#F7F8FA;--surface:#FFFFFF;--edge:#E2E6EB;
  --edge-soft:#EDF0F3;--ink:#14171B;--muted:#5B636E;--faint:#8B929C;}

/* --accent resolves ON the lane, never on :root. --a and --a-dark are set per
   lane by the inline style, so a :root declaration of var(--a) computes against
   an undefined variable, becomes guaranteed-invalid, and inherits down dead:
   no accent border, no filled nodes, no connector lines. */
.lane{--accent:var(--a);}
@media (prefers-color-scheme:dark){.lane{--accent:var(--a-dark);}}
:root[data-theme="dark"] .lane{--accent:var(--a-dark);}
:root[data-theme="light"] .lane{--accent:var(--a);}

body{background:var(--ground);color:var(--ink);font-family:var(--sans);
  line-height:1.45;-webkit-font-smoothing:antialiased;}
.wrap{max-width:1000px;margin:0 auto;padding:34px 22px 70px;
  display:flex;flex-direction:column;gap:22px;}

.page-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 14px;
  padding-bottom:15px;border-bottom:2px solid var(--ink);}
.page-head h1{margin:0;font-size:1.45rem;font-weight:700;letter-spacing:-.02em;}
.page-head .count{font-family:var(--mono);font-size:.72rem;text-transform:uppercase;
  letter-spacing:.1em;color:var(--faint);}
.page-head .gen{margin-left:auto;font-family:var(--mono);font-size:.68rem;color:var(--faint);}

.lane{background:var(--surface);border:1px solid var(--edge);border-radius:4px;
  border-left:4px solid var(--accent);padding:16px 18px 14px;
  display:flex;flex-direction:column;gap:12px;}
.lane header{display:flex;flex-wrap:wrap;align-items:baseline;gap:5px 11px;}
.lane .co{font-weight:700;font-size:1rem;letter-spacing:-.01em;}
.lane .co a{color:inherit;text-decoration:none;border-bottom:1px solid var(--edge);}
.lane .co a:hover,.lane .co a:focus-visible{border-bottom-color:var(--accent);}
.lane .role{color:var(--muted);font-size:.86rem;}
.lane .age{margin-left:auto;font-family:var(--mono);font-size:.7rem;
  color:var(--faint);font-variant-numeric:tabular-nums;white-space:nowrap;}

/* Wide tracks scroll inside their own box so the page never scrolls sideways. */
.track-wrap{overflow-x:auto;padding-bottom:2px;}
.track{display:flex;align-items:flex-start;width:max-content;padding:5px 0 2px;}

/* Fixed link width, deliberately not flex-grow: a two-round loop must not
   render as wide as a five-round loop and imply the same distance covered. */
.link{flex:0 0 46px;height:3px;margin-top:9px;background:var(--edge);border-radius:2px;}
.link.done{background:var(--accent);}

.stop{flex:0 0 112px;display:flex;flex-direction:column;align-items:center;gap:9px;}
.stop i{width:21px;height:21px;border-radius:50%;box-sizing:border-box;
  border:2px solid var(--edge);background:var(--surface);display:block;}
.stop .lbl{display:flex;flex-direction:column;align-items:center;gap:2px;}
/* The frame caption is what the eye scans across lanes, so it stays legible
   even on a future stop; the detail is deliberately quieter than its caption. */
.stop .cap{font-size:.74rem;line-height:1.25;text-align:center;color:var(--muted);
  letter-spacing:-.005em;}
.stop .det{font-size:.63rem;line-height:1.3;text-align:center;color:var(--faint);
  overflow-wrap:anywhere;}
.stop.future .cap{color:var(--faint);}
.stop.done i{background:var(--accent);border-color:var(--accent);}
.stop.done .cap{color:var(--muted);}
.stop.here i{background:var(--surface);border-color:var(--accent);border-width:4px;
  animation:pulse 2.1s ease-out infinite;}
.stop.here .cap{color:var(--ink);font-weight:680;}
.stop.here .det{color:var(--muted);}
.stop.terminal i{border-radius:3px;transform:rotate(45deg);}

@keyframes pulse{
  0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--accent) 55%,transparent);}
  70%{box-shadow:0 0 0 9px color-mix(in srgb,var(--accent) 0%,transparent);}
  100%{box-shadow:0 0 0 0 color-mix(in srgb,var(--accent) 0%,transparent);}
}
@media (prefers-reduced-motion:reduce){.stop.here i{animation:none;}}

.next{margin:0;font-size:.85rem;color:var(--muted);}
.next.none{color:var(--faint);font-style:italic;}

.empty{background:var(--surface);border:1px dashed var(--edge);border-radius:4px;
  padding:34px 20px;text-align:center;color:var(--muted);}
.foot{font-size:.75rem;color:var(--faint);text-align:center;}
"""


def render(rows, generated_at=None):
    """Build the full page. `rows` are jobs in the interviewing state."""
    lanes = [lane_model(r) for r in rows]
    stamp = (generated_at or datetime.now()).strftime("%b %-d, %Y at %-I:%M %p")

    if lanes:
        body = "".join(_lane_html(l, i) for i, l in enumerate(lanes))
        count = f"{len(lanes)} live"
    else:
        body = ('<div class="empty">No live conversations. Nothing is in the '
                'interviewing state right now.</div>')
        count = "none live"

    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Interviews</title><style>" + CSS + "</style></head><body>"
        '<div class="wrap"><header class="page-head"><h1>Interviews</h1>'
        f'<span class="count">{html.escape(count)}</span>'
        f'<span class="gen">{html.escape(stamp)}</span></header>'
        + body +
        '<p class="foot">Rounds are per job and in the order they actually '
        'happened. Generated by job-hound.</p>'
        "</div></body></html>"
    )


def write(rows, path, generated_at=None):
    """Render and write the page. Returns the Path written."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render(rows, generated_at=generated_at), encoding="utf-8")
    return p
