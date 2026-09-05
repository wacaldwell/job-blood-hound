# Interview stage board design

Date: 2026-08-07
Status: approved, not yet implemented

## Why

job-hound tracks a job's lifecycle but stops modeling it exactly where the job
gets interesting. `interviewing` is a single flat state. On the live database
today, Vertex Analytics (all rounds finished, waiting on an offer or a rejection) and
Initech (peers and culture fit done, technical round still unscheduled) are
indistinguishable: same state, same row shape, no way to tell them apart without
opening a Notion dossier. Contoso Health, which has a recruiter screen scheduled, is
still sitting in `applied`.

So the one question worth asking during an active search, "where does each
conversation actually stand," is the one question the system of record cannot
answer. It lives in the operator's head and in scattered prep documents.

This adds the missing layer: a per-job list of interview rounds, a marker for
where he is in it, and a browser page that renders the active loops as
horizontal transit lines.

The visual is the point, not a side effect. The objective is a page that is
pleasant to open, not a status dump. A terminal renderer was considered and
explicitly rejected.

## Rounds are per-job data, not a schema constraint

The first modeling attempt used a fixed ordered ladder of typed stations
(screen, hiring manager, technical, final, decision). It is wrong, and Initech is
the counterexample that killed it. Round 3 was booked as the technical, then the
recruiter swapped it for a peer and a TPM because of interviewer availability,
and the technical became round 4 and is still unscheduled. A fixed ladder that
puts `technical` before `final` renders Initech incorrectly and cannot be
corrected without a migration.

Note what caused the reorder: a calendar, not a process. Any model that treats
round order as a property of the company's hiring process will be wrong the
first time somebody is on vacation.

What is actually true is narrower: **the first round is usually a hiring manager
conversation, and everything after that varies in count, type, and order.** That
is a default worth prefilling, not a rule worth enforcing.

So each job carries its own ordered list of rounds, each with a free-text label.
The board renders whatever that list says. A new job's list is seeded with the base
frame, but any list can be reordered, extended, trimmed, or
relabeled without a schema change. This handles a take-home before the hiring
manager and a six-round loop as ordinary data.

**The base frame is `recruiter | round 1 | round 2 | round 3 | decision`.** That
skeleton is what a new application starts on, so lanes stay comparable at a
glance, and `decision` is appended at render time rather than stored.

**Captions are derived from position, never stored.** Each node draws its frame
caption (recruiter, round 1..N, decision) from where it sits, and the stored
label renders underneath as small print: who was actually in that round. This is
the structural fix for the one visible bug here, where Vertex Analytics carried a
placeholder labelled "round 3" while sitting in the second slot, so the label
named one number and the position named another. With captions positional, that
disagreement is now unrepresentable rather than merely corrected.

A detail identical to its caption (an unfilled "round 2" seed) is dropped rather
than printed twice. A loop that runs two rounds or five edits its own list.

Round numbers are position in the list.

## Stage semantics

A job has a round list and a marker.

- Marker at round N means rounds 1 through N-1 are complete and round N is the
  active or upcoming one.
- Marker at `decision` means every round is complete and the outcome is pending.
  `decision` is not stored in the round list; it is appended automatically as
  the terminal node when rendering.

Whether the marked round has been scheduled yet is carried by the free-text
`interview_next` line ("final round, not yet scheduled"), not by another status
field. That distinction matters to read but does not earn its own column.

**Outcomes are not rounds.** Offer and rejection are already modeled: a job
becomes `closed` with an outcome. A closed job leaves the board. Rounds live
inside `interviewing`, outcome lives in `state`, and the two never overlap. This
keeps the existing state machine authoritative and stops the board from becoming
a second, competing lifecycle.

## Schema

Additive migration on the jobs table, applied automatically the next time
`JobDB` opens on the host, following the same pattern the gate columns used:

| Column | Type | Meaning |
| --- | --- | --- |
| `interview_rounds` | TEXT | JSON array of round labels, in order |
| `interview_at` | INTEGER | 1-based marker position, or NULL |
| `interview_decision` | INTEGER | 1 when the marker is at `decision` |
| `interview_next` | TEXT | free text, what is actually next |
| `interview_updated` | TEXT | ISO timestamp the marker last moved |

Storing the round list as JSON on the row follows the `gate_json` precedent
rather than introducing a rounds table, because nothing queries across rounds:
the list is always read and written whole, for one job at a time.

`interview_next` carries the nuance the labels cannot. The round list gives each
lane its stations; this line gives it its truth.

No backfill. Three jobs need seeding by hand after deploy.

## Writing

All columns are written through setters in `jobdb.py` and audited to
`state_log`, so `jobdb.py` remains the only writer and the audit trail stays
complete. No new writer, and no write path that bypasses the audit.

## CLI

```
jh rounds <ident>                          show the current list
jh rounds <ident> "HM,panel,tech"          replace the list
jh rounds <ident> --add "technical"        append one round
```

Labels are free text. Replacing a list shorter than the current marker position
moves the marker to the last round rather than leaving it dangling past the end.

```
jh stage <ident> <n|decision> [--next "..."]
```

Moves the marker. `<n>` must be within the round list; anything outside it
errors rather than silently extending the list. `<ident>` resolves the same way
it does everywhere else (full uid, slug, or unique slug prefix).

If the job has no round list yet, `jh stage` seeds the default
frame first, so a job whose loop nobody has described yet still lands on the
standard skeleton.

State interaction:

- job in `applied`: sets the marker and auto-transitions to `interviewing`,
  recorded as its own audited `state_log` line. This is a legal transition and
  saves a second command after a screen actually happens.
- job in `interviewing`: sets the marker, no transition.
- any other state: errors. The command does not guess, and it never moves a job
  backward out of a terminal state.

`--next` is optional. Omitting it clears any previous note rather than silently
keeping a stale one, because a stale "next" is worse than none.

```
jh board [--open]
```

Selects `state = 'interviewing'` and renders every such job. Writes one
self-contained HTML file into `JOB_APPS_DIR` on the host, which means `jh-pull`
already carries it down to the Mac alongside the packages. No new transport, no
publish step, no second sync path.

`--open` additionally opens it locally.

A job in `interviewing` with no round list still renders, as a lane with the
default stations and no marker placed. Hiding it would make the board quietly
lie about how many conversations are live.

## Rendering

One lane per job, horizontal, on a dark ground. Per lane:

- Company and role above the line.
- One node per round, in list order, plus a terminal `decision` node. Rounds
  before the marker filled, the marked round highlighted and pulsing, rounds
  after it hollow.
- The round label under each node, wrapping rather than truncating, since the
  labels are free text and can be long ("peers + culture fit").
- The `interview_next` line below the lane, in a quieter weight.
- Days since `interview_updated` as a small figure at the right. A lane that has
  not moved in three weeks should read differently from one that moved
  yesterday, and that difference should be visible without arithmetic.

Lane width is driven by round count, so a four-round loop and a two-round loop
do not render at the same scale and imply equal progress. Each lane gets its own
accent so the eye can separate them.

Theme-aware via tokens (light and dark both designed, not inverted). Responsive:
lanes stack and labels shorten on narrow screens rather than overflowing. No
external assets, no CDN fonts, no network calls, so the file works from disk.
`prefers-reduced-motion` disables the pulse.

## Testing

Against mocked data, per the repo's existing convention:

- `jh rounds` replaces, appends, and shows a list
- replacing with a shorter list clamps a marker that would dangle past the end
- `jh stage` rejects a marker outside the round list
- `jh stage` on a job with no list seeds the default template first
- `applied` auto-transitions to `interviewing` and writes both audit lines
- `interviewing` sets the marker with no transition
- a job in any state other than `applied` or `interviewing` errors rather than
  transitioning (checked against `closed` and `drafted`)
- omitting `--next` clears a previously set note
- `decision` renders as a terminal node and is never stored in the round list
- board renders correctly at zero jobs, one job, and several
- a job with no round list renders as a default lane with no marker
- the written HTML references no external URLs

The suite's existing `conftest.py` fixtures already isolate `JOB_DB` and
`JOB_APPS_DIR`, so board rendering cannot write into the real applications
directory during tests. Both fixtures stay.

## Delivery

Feature branch, PR, `tests` green, merge. The deploy runner restarts services on
any `.py` change and the additive migration applies on the next `JobDB` open, so
there is no manual deploy step.

Seeding after deploy, using the real loops:

```
jh rounds vertexanalytics "hiring manager + principal engineer,round 3"
jh stage  vertexanalytics decision --next "awaiting offer or rejection"

jh rounds bamboo "hiring manager,peer + TPM,technical"
jh stage  bamboo 3 --next "final round, not yet scheduled"

jh rounds contosohealth "hiring manager,panel: cloud engineers,skip-level"
jh stage  contosohealth 1 --next "recruiter recommending; not yet scheduled"
```

Initech's labels are confirmed. Vertex Analytics's round 3 label should be checked
against its Notion dossier at seeding time rather than taken from this document.
