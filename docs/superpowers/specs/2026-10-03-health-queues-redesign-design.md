# Health & queues page redesign

## Problem

Pipeline → Health & queues (`/pipeline/control`, `views/ControlPanel.tsx`)
stacks eleven sections of equal weight: a trip banner, a chip box, AI
capacity, phase cards, a machine table, a job list with a paragraph per job,
three history widgets with three range pickers, two queue tables, the local
job table, and the decision-capture box. An operator who opens it cannot tell
what has run. The one table that lists runs, Recent jobs, shows only the jobs
started from this app, at the bottom of the page. What each worker machine did
is spread across the auto-hunt cards, the verify and fix histories, and the
machine table, and the runs ledger does not record which machine wrote most of
its rows, so no view can say it.

## The page

Top to bottom. Every section is a bordered panel with a shaded header band, so
sections read as separate blocks. A folded panel shows a one-line summary in
its header and remembers per browser whether it is open (`localStorage`, every
access wrapped in try/catch, the default applies when storage is unavailable).

| Panel | Default | Content |
|---|---|---|
| Running job line | shown only while a job runs | `<job> · running 2m 10s · Stop · output ▾`; the console opens under it |
| Needs you | open; red header when it has items | one line per problem, details expand under it |
| Recent activity | open | one block per machine for the past 24 hours |
| Queues | open | Security, Verify, Fix cards; each opens its table |
| Pipeline coverage | open | the phase cards with progress bars, three per row |
| Run a job | open | recommended jobs full width, the rest as tiles |
| AI capacity | folded | the account cards as today |
| Run history | folded | one merged table |

The page's intro paragraph is removed, and the decision-capture box moves to
the Data tab (`views/Tables.tsx`).

### Needs you

One line each, from data the page already loads:

- a tripped lane (`autohunt.status.health`): `<lane> lane paused on <host> ·
  <kind> · <ago>`, a Resume button, and the reason, remedy and last self-test
  behind the expander;
- an offline worker (the activity endpoint's machine status);
- a failing or stale base pin (`autohunt.status.base`);
- background AI paused (`/api/capacity`, an account whose decision is not
  allowed), with its resume time;
- failed hunter runs (`groupNeedsAttention`): `N failed runs across M reasons`,
  expanding to today's chips.

With none of these it is one line, `All lanes healthy`. Stale ingests are not
listed here: the recommended Ingest job carries them.

### Recent activity

One block per machine: name, `this machine` when it serves the app, status
(`● online`, `offline 3h`, or the paused lanes), the PR or issue it is on now,
and one sentence:

> 8 security reviews (4 GREEN, 4 YELLOW) · 3 verifies (1 verified, 2 not
> verified) · 330 autofix attempts: 2 pushed, 5 parked, 18 refused, 304 failed ·
> 6 issue fixes · background: 71 PR watches, 60 threat scans, 40 re-review
> requests · you ran Ingest, Threat scan · $41 AI

A phrase with nothing to say is left out; a machine with no phrase reads
`idle`. Each lane phrase links to its PRs in the PR Explorer
(`{numbers, state: "all"}`, as the auto-hunt chips do), as does each outcome
inside it. A job this app ran opens that job's output in the Run a job panel.
Rows without a machine name are grouped under `unattributed`; they exist only
for rows written before a machine runs this change, and they leave the window
within a day. A held security verdict writes no ledger row and is not counted
here; the tripped lane in Needs you reports it.

### Queues

Three cards:

- Security: the hunter's pool awaiting review
  (`autohunt.status.security_pool`).
- Verify: running, queued and waiting-for-base counts, and the GREEN pool
  awaiting verify. Opens the verify queue table as today.
- Fix: running, awaiting review, and ended within half an hour. Opens the fix
  queue table as today, with Push, Discard and Escalate. It opens by itself
  when a change awaits review.

### Run a job

Recommended jobs come first, full width: name, a `recommended` tag, the
suggestion's reason, the job's description, its inputs, last run, typical
duration, and Run. Every other job, with Refresh live state and Scan for
responses, is a tile in a grid (`repeat(auto-fill, minmax(150px, 1fr))`)
showing its name. Clicking a tile opens it full width beneath the grid with the
same detail; one tile is open at a time. A running job's tile is marked.

### Run history

One table of security, verify and autofix runs (the autohunt history and the
fix history merged, newest first), with lane filter chips, the per-result
summary chips, and one range picker (7, 30, 90 days, all time) driving both
fetches. The separate verify run history goes: its rows are the autohunt
history's verify rows.

## Backend

### Every ledger row names its machine

`storekit.stamp_host(record)` sets `record["host"] = settings.worker_id()` when
the record has no top-level `host`. The four ledger writers call it before
inserting: `Store.append_run`, `IssueStore.append_run`, `AlertStore.append_run`,
`AdvisoryStore.append_run`. `append_agent_run` already writes `host`. The field
is additive: older readers ignore it, so `STORE_SCHEMA_VERSION` does not move.
Readers take the machine as `record.host`, else `record.stats.host` (where
`fix:single` and `issue-fix:run` put it today).

### `GET /api/machines/activity`

`prospector_app/backend/machine_activity.py`. A pure `summarize(rows,
agent_runs, roster, local_jobs, local_host, now)` over plain data, and an
`activity()` that gathers its inputs: the `pr`, `issue` and `alert` ledgers
through the app's cached reads (`pipeline_status._ledger_records`), the agent ledger
(`Store.agent_runs(since)`), `machines.roster()`, and `jobs.list_jobs()`.

The window is the last 24 hours by each row's `finished`, else `started`. Each
row lands in one bucket:

| Bucket | Phases | Outcome |
|---|---|---|
| security | `security:review-one` | `stats.verdict` |
| verify | `verify:single` | `stats.outcome` when `status` is `done`; `waiting for base` when `waiting-for-base`; `error: <error_kind>` when `error` |
| autofix | `fix:single` | `stats.status`, with `awaiting-review` read as `parked` |
| issue fix | `issue-fix:run` | `stats.ending` |
| job | a `jobs.JOB_SPECS` ledger phase outside the four lanes, whose `trigger` is not `worker`, `autohunt` or `hunter` | the job's label |
| background | everything else in a fixed label map (`ingest:watch` → PR watches, `threat-scan:heads` → threat scans, `rereview:request` → re-review requests, `cluster:*` and `analyze:commit` with `trigger: worker` → clustering passes, `verify:pin-refresh` → base pin refreshes, `reingest` → re-ingests, `threat-evidence:capture` → evidence captures) | none |

Rows of other phases (store edits, `worker:trip`, replay and eval runs) are not
counted. AI spend is the sum of `cost_usd` over the machine's agent runs in the
window.

For the machine serving the app, jobs come from `jobs.list_jobs()` in the
window instead of the ledger, so a job that failed reads as failed and carries
its id for the output link.

Response:

```
{local, window_hours: 24, machines: [{
  host, local, online, offline_since, tripped: [lane], current: {pr, issue},
  lanes: {security|verify|autofix|issue_fix: {count, outcomes: [{label, count, prs}], prs}},
  background: [{label, count}],
  jobs: [{label, kind, status, job_id}],
  spend_usd}]}
```

Machines are the roster's plus any host seen in the window, online first, with
`unattributed` last when it has rows. The page polls it every 30 seconds.

### Recommended jobs

`GET /api/actions/suggested?view=all` returns the `prs`, `issues` and `alerts`
suggestions together. `pr_suggestions` gains one: `cluster-new` when
`coverage.not_clustered > 0` and the newest `cluster:summaries` or
`cluster:assign` row is older than `STALE_AFTER_HOURS` (24h), reason
`N PRs are not in a cluster (last clustering <ago>)`, count `CLUSTER_BATCH`. On
a deployment whose clustering lane runs hourly the age condition keeps it
quiet. It shows on the PRs tab's suggestions too.

## Frontend structure

`views/ControlPanel.tsx` becomes the page shell; the panels move to
`views/control/`:

- `Panel.tsx`: the bordered section, header band, tone, fold with remembered
  state.
- `NeedsYou.tsx`, `RecentActivity.tsx`, `Queues.tsx` (with the verify and fix
  queue tables moved as they are), `Coverage.tsx` (`PhaseCard` as it is),
  `Jobs.tsx`, `Capacity.tsx`, `RunHistory.tsx`.
- `useJobRunner.ts`: the job stream state the running-job line and the Jobs
  panel share.
- `format.ts`: `ago`, `fmt`, `fmtDuration`, `fmtElapsed` and the chip-tone
  helpers.
- Pure, tested: `activitySentence.ts` (endpoint machine → phrases),
  `runHistory.ts` (merge, sort, filter), `jobLayout.ts` (recommended first, the
  rest as tiles).

## Testing

- `storekit.stamp_host`, and each of the four writers stamping when `host` is
  absent and keeping one that is present.
- `machine_activity.summarize`: the window edge, each bucket's outcome mapping,
  background labels, the job rule's trigger filter, local jobs replacing
  ledger jobs for the local machine, the unattributed bucket, an offline
  roster machine with no rows, spend.
- `pr_suggestions`: `cluster-new` with and without a recent clustering row.
- An endpoint smoke test.
- `node --test`: `activitySentence` (plurals, empty phrases, idle, offline,
  unattributed), `runHistory`, `jobLayout`.
- Gates: pyright, ruff, pytest, `pnpm run build`, eslint on touched files. The
  page is checked in the preview with every worker lane off in the preview's
  environment.

CLAUDE.md gains one line under the store: every ledger row names the machine
that wrote it (`host`).
