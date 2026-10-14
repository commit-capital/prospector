# Health & queues: lanes, now, next

Supersedes the page layout in `2026-10-03-health-queues-redesign-design.md`.

## Problem

Pipeline → Health & queues (`views/ControlPanel.tsx`) answers the wrong
questions first. An operator wants five things at a glance: which lanes are
on, what is running now, what runs next (and to change that), what already
ran, and what is broken. The page instead opens on Needs you, which reads a
machine the operator stopped on purpose as `worker offline`. Recent activity is
one dense sentence per machine and only looks back. Queues shows three of the
lanes (Security, Verify, Fix) as long tables. Coverage, jobs, capacity and
history follow at equal weight.

Today no lane can be paused short of editing `.env`. A queue has no priority,
so its order is fixed: operator before auto, then oldest. Nothing records when
a periodic loop runs next, and no endpoint serves a worker's log.

## The page

Top to bottom:

| Section | Content |
|---|---|
| Status line | one sentence: which machines are working, which are off, whether background AI is waiting |
| Needs you | only problems a person must act on; absent when there are none |
| Lanes | one row per lane: state per machine, now, next, 24h count, pause |
| Timeline | upcoming scheduled passes, what runs now, what just ran |
| Tabs | Coverage and jobs · AI capacity · Run history |

The running-job line (`JobLine`) stays above the status line while a job
started from this app runs.

### Status line

Derived from the overview (below): `Mac Studio working on 2 lanes · Macbooko
off · background AI waits for the weekly reset (~11 PM)`. Each machine reads
`working on N lanes`, `idle`, `off` (stood down, below), or `offline 3h`
(heartbeat silent, not stood down).

### Needs you

One row each: a headline, how long ago, and one action button.

- a tripped lane on a machine that is not stood down: **Resume**;
- a worker offline on a machine that is not stood down: **Stand down**
  (marks the machine off on purpose, below);
- a failing or stale base pin: **Details**;
- failed hunter runs, grouped by reason: **Open** (PR Explorer, as today);
- parked fix changes awaiting approval: **Review** (expands the Autofix lane).

A lane turned off by its flag is not a problem and never appears here; it
reads `off` in Lanes. Background AI waiting on a usage limit is not here
either: it is the status line and the lanes' `AI paused` state, since the
operator cannot act on it. Each row can be muted for 24 hours (per browser,
`localStorage`, guarded by try/catch); a muted count shows in the header.

The red health strip atop every page (`useSystemHealth`, `system_health.status`)
reads the same filter: a stood-down machine contributes neither its offline nor
its tripped lanes.

### Lanes

Rows, in order: Security, Verify, Design, Autofix, Issue fix, Clustering, and
one Background row folding threat refresh, PR watch, stale refresh and
re-review.

| Column | Content |
|---|---|
| Lane | name |
| State | per machine: `on`, `off` (flag), `paused` (operator), `tripped`, `AI paused`, `stood down`; a lane on no machine reads `off` |
| Now | the item in flight: PR or issue, step, elapsed; click to open its log (local machine only) |
| Next | the queue head, or `next run in 6m` for a periodic lane, or why nothing is next (`pool empty`, `retest in 9m`, `after ~11 PM`) |
| 24h | runs ended in the past day, with a red count of machine faults |
| Control | pause / resume on this machine |

Clicking a row expands it in place:

- **Queue** (Verify, Autofix, Issue fix): the queued items in the order the
  worker will take them, with **Move to top** and **Remove**. Autofix keeps its
  parked changes here with Push and Discard, as the Fix table does today.
- **Hunter's next picks** (Security, Verify, Design, Autofix): the next five
  candidates the hunter would take, read-only.
- **Recent**: this lane's last ten endings, one line each.

Clustering shows its pending count and daily budget used. Background lists
each loop with its last and next run.

### Timeline

One list, newest at the top of the past and soonest at the top of the future,
split by a `now` divider:

- **Upcoming:** each enabled periodic pass's next run, and each lane's queue
  head marked `next`.
- **Now:** every item in flight on every machine.
- **Past:** ledger endings from the past 24 hours, one line each:
  `12:31 · Security · #11340 → GREEN · 4m · Mac Studio`. Machine faults read
  red. Background passes that did nothing collapse into one line per hour.

Clicking a past line opens its PR or issue, or its job output as today
(`jobToOpen`). This replaces Recent activity; the per-machine cost moves to
the AI capacity tab.

### Tabs

Coverage and jobs (the coverage cards and Run a job, as today), AI capacity,
Run history. The selected tab is remembered per browser.

## Machines off on purpose

A machine is **stood down** while its registry row `machine_mode:<host>`
holds `{stood_down: true, at, by, note}` and no worker on it has beaten since
`at`. A heartbeat after `at` ends it, so a laptop that comes back reads
working again without a click.

- `POST /api/machines/{host}/stand-down` `{note?}` and
  `POST /api/machines/{host}/stand-up` write and clear it, as the operator.
- `escalation.offline_workers` and `system_health.summarize` skip a
  stood-down host. Its tripped lanes are kept in the roster but excluded from
  Needs you and the strip.
- The status line and Lanes read `off` for it. The machine block in Setup →
  Machines offers the same toggle.

## Operator pause

`lane_pause:<host>` holds `{lanes: {<lane>: {at, by}}}`. A paused lane takes no
new work, operator-queued or hunted, including the push of a change the
operator approved; the item in flight finishes.

- `lane_health.open_or_retest(lane)` returns false for a paused lane, before
  any retest, so every pick gate (`verify_worker`, `fix_worker`,
  `issue_fix_worker`, `cluster_refresh`) honors it with no change of its own.
  The periodic background loops are not pausable here.
- `POST /api/worker/lanes/{lane}/pause` and `/resume` `{host}` write it. A
  pause is per machine; the Lanes control acts on this machine, and the
  expanded row lists every machine's.
- A paused lane is not a trip: it books no failure, retests nothing, and
  never appears in Needs you.

## Queue order

`verify_request`, `fix_request` and the issue `fix_request` gain an optional
`priority: float`. `next_queued` / `next_request` sort by `priority`
descending (absent reads 0) ahead of their present keys, which are unchanged.

- **Move to top** sets `priority` to one more than the queue's highest.
  `POST /api/verify/queue/{pr}/top`, `/api/fix/queue/{pr}/top`,
  `/api/issues/{n}/fix/top`.
- **Remove** is the existing dequeue / cancel.
- The field is ignored by code that does not read it, so no schema bump: an
  older worker drains in its old order.
- Hunter picks are not a queue and are not reorderable.

## Overview endpoint

`GET /api/control/overview` (`prospector_app/backend/control_overview.py`)
returns everything the status line, Needs you, Lanes and Timeline need in one
read, built from what exists:

- per host: heartbeats (`machines.roster`), stood-down state, lane flags
  where the host reports them, trips (`escalation.health_status`), pauses;
- per lane: in-flight items with step and start (`/api/status/now`'s
  source), queue in worker order, hunter's next five
  (`verify_worker.next_auto` / `fix_worker.next_auto` ranking, read-only),
  24h endings by outcome;
- periodic passes: last run from the ledger phase
  (`ingest:watch`, `threat-scan:heads`, `rereview:request`, the cluster
  phases, and a new `ingest:stale-refresh` phase `stale_refresh` books per
  pass), next run as last run plus the loop's interval
  constant, only for loops enabled on some online machine;
- the Needs you items, already filtered.

Each worker heartbeat adds the lane flags its process runs under
(`worker_control.flags()`), so a remote machine's `off` is known rather than
guessed. The page polls this one endpoint every 15 s (5 s while anything runs)
in place of its six polls.

## Log tail

`GET /api/worker/log?after=<offset>` returns new lines of this machine's
`worker_log.path()` with the next offset; the page polls it every 2 s while a
Now cell's log is open. The issue-fix worker and the cluster lane install
`worker_log` as verify and fix do. A remote machine's Now cell shows step and
elapsed with no log link.

## Delivery

Three PRs:

1. **Layout and overview.** `control_overview.py` and its endpoint, heartbeat
   flags, stand down / stand up, the filtered Needs you and health strip, the
   new page (status line, Lanes without pause or reorder, Timeline, tabs).
   Removes `NeedsYou`, `RecentActivity` and `Queues` as page sections.
2. **Pause and reorder.** `lane_pause`, the `open_or_retest` check, the
   `priority` sort, the move-to-top endpoints, the controls.
3. **Log tail.** The endpoint, `worker_log` for issue-fix and cluster, the
   Now cell's log.

## Testing

- `control_overview`: a lane off by flag, paused, tripped, and stood down
  each read their state and only the tripped one on a working host is in Needs
  you; next run is last ledger row plus interval and absent for a loop no
  online machine runs.
- Stand down: offline and trips are suppressed; a beat after `at` clears it.
- Pause: `open_or_retest` false while paused, no failure booked, true on
  resume.
- Priority: `next_queued` takes a moved item first; absent priority keeps the
  present order.
- Log tail: offsets resume and survive rotation to `.log.1`.
- Frontend: `pnpm run build`, no new lint errors, and the page checked in the
  preview against a scratch store.
