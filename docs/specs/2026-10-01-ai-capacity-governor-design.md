# AI capacity governor — design

Date: 2026-10-01. Sub-project 1 of 3 toward unattended pipeline operation
(2: a pipeline lane that runs ingest/cluster/analyze on a cadence; 3: headless
PR clustering). This document covers sub-project 1 only.

## Problem

Every unattended worker (the security/verify hunter, autofix, issue-fix)
starts agent runs whenever its queue is empty, with no notion of how much AI
capacity it is spending. Agents run on the operator's own Claude subscription
(Max), whose 5-hour and weekly windows are shared with the operator's
interactive use and across every machine logged into that account. Today:

- Nothing stops background work from consuming the window the operator needs
  for their own work, or from exhausting the weekly limit early in the week.
- The CLI reports the account's live window utilization on every run (a
  `rate_limit_event` in the stream) and per-run usage and cost (the `result`
  event); `headless_agent.run_agent` discards both.
- A usage-limit hit raises `AgentUnavailable`, the same error as "not logged
  in": the worker trips every lane as an outage and retests every 15 minutes
  with a Haiku probe that can pass while the Opus limit is still spent.
- Different operators (Brandon, Devin, Nicky…) may each provision machines on
  their own subscription or on a pay-per-token API key; there is nowhere to say
  how much of each one Prospector may use.

## Goals

1. Background agent work spends only the capacity each account's owner grants:
   for a subscription, a cap on the 5-hour window's utilization that depends on
   the time of day, plus pacing of the weekly window; for an API key, a daily
   dollar budget.
2. The policy belongs to the AI account, is set when a machine is provisioned
   (Setup tab), and is shared by every machine on that account.
3. One gate, consulted by every unattended lane before it starts agent work;
   work an operator starts is never gated.
4. A usage-limit hit pauses background work until the window resets instead of
   tripping lanes.
5. The operator can see each account's window, the cap in effect, whether
   background work is paused and why, and what it spent.

## Non-goals

- Scheduling the ingest/analyze phases (sub-project 2) and headless PR
  clustering (sub-project 3).
- Detecting the operator's interactive activity. Day/night hours stand in for it.
- Interrupting an agent run already in flight. The cap governs starts.
- Gating the chat pane or any run an operator starts or queues.

## Terms

- **Account** — the AI account a machine's Claude CLI is logged into, as
  `claude auth status --json` reports it.
- **Reading** — one observation of an account's windows: 5-hour and 7-day
  utilization (0–1) with their reset times, and a status.
- **Unattended** — agent work the system starts on its own: hunter picks and
  the agent steps of work the automation queued. Its opposite is
  operator-started work.

## Design

### 1. Accounts and readings

**Account detection.** `capacity.account()` runs `claude auth status --json`
once per process (cached; refreshed when a lane retests) and returns:

- `key` — the first 16 hex chars of sha256 over `authMethod|orgId|email`. When
  neither org id nor email is present, sha256 over `machine|<worker id>`, so an
  unidentifiable account is never pooled with another machine.
- `billing` — `subscription` when `authMethod == "claude.ai"`, else `api`.
- `plan` — `subscriptionType` (e.g. `max`), when present.
- `label` — the masked email (`br…@gmail.com`) plus the plan, e.g.
  `br…@gmail.com · Max`; the raw email is never stored.

A failure to run the command yields no account; the gate treats that as
unknown (paused).

**Readings from the stream.** `headless_agent.parse_stream` passes
`rate_limit_event` events to a new `on_rate_limit` callback, and `run_agent`
keeps the last one. Its `rate_limit_info` yields: `status` (`allowed`,
`allowed_warning`, `rejected`), `rateLimitType`, `resetsAt`, and
`unifiedWindows.five_hour` / `unifiedWindows.seven_day`, each
`{utilization, resetsAt}`. `run_agent` records the reading for this machine's
account after every run, success or failure, and the run's `result` usage
(below).

**Shared state.** Two registry rows per account, so machine writes never
clobber an operator's policy edit:

- `ai_account:<key>` — the policy (section 2), the label, billing and plan.
  Written only by the policy endpoint.
- `ai_capacity:<key>` — `{reading: {five_hour: {utilization, resets_at},
  seven_day: {utilization, resets_at}, status, at, by}, paused_until,
  paused_reason}`. Written by machines through a conditional upsert that only
  replaces a reading when the incoming `at` is newer (`ON CONFLICT DO UPDATE …
  WHERE`), so the newest observation from any machine on the account wins
  atomically. A pause is written the same way, keyed on its own timestamp.

**Freshness.** A reading older than `READING_MAX_AGE` (10 minutes) is stale.
When the gate needs a decision and the newest reading is stale, the machine
runs `headless_agent.probe()` (one Haiku call) and records its reading. On an
active machine real runs keep the reading fresh and the probe rarely fires.

**Fail-safe.** No account, no reading, a reading without `unifiedWindows`, or a
CLI that stops emitting `rate_limit_event` are all *unknown*: background agent
work pauses and says why. Operator-started work is unaffected.

### 2. Policy

Subscription accounts:

| Field | Default | Meaning |
|---|---|---|
| `timezone` | the machine's local IANA zone | the zone `day_start`/`day_end` are read in |
| `day_start` | `08:00` | when daytime begins |
| `day_end` | `23:00` | when daytime ends |
| `day_cap` | `0.50` | max 5-hour utilization at which background work may *start*, daytime |
| `night_cap` | `0.90` | the same, outside daytime |
| `weekly_pacing` | `true` | hold weekly utilization to the elapsed share of the week |

Pacing slack is a constant, `WEEKLY_SLACK = 0.05`. The weekly window's start
is read from the reading itself (`seven_day.resets_at − 7 days`), so each
account paces against its own weekly reset; the Setup card shows that reset,
read-only.

API-key accounts: `daily_budget_usd` (no default) and `timezone`; the budget
resets at local midnight.

**Where it is set.** The Setup tab's worker section gets an **AI capacity**
card for this machine's account: its label, the machines that share it, its
policy fields, and its weekly reset. Saving writes `ai_account:<key>` via
`PUT /api/capacity/policy`, which edits only the account of the machine serving
the request (validated: times parse, `0 < day_cap, night_cap ≤ 1`, a known
timezone, a non-negative budget). Other accounts appear read-only in the
Machines list.

**Missing policy.** A subscription account with no saved policy runs on the
defaults; the card shows "using defaults — review". An API-key account with no
budget runs no unattended agent work until one is set, and the health strip
says so.

### 3. The gate

`capacity.check(account) -> Decision{allowed, reason, retry_at}`, rules in
order:

1. `paused_until` in the future → paused until then (a limit hit, section 4).
2. No fresh reading after a probe → paused, `retry_at` = now + 10 minutes.
3. Subscription, 5-hour utilization ≥ the cap in effect now → paused;
   `retry_at` = the earlier of the window's reset and the next policy boundary
   (e.g. `day_end`, when the cap rises).
4. Subscription with pacing, weekly utilization > elapsed share + slack →
   paused; `retry_at` = when the elapsed share catches up.
5. API key, today's background spend ≥ `daily_budget_usd` → paused until local
   midnight.
6. Otherwise allowed.

**Unattended work.** `capacity.unattended(lane)` is a context manager setting a
ContextVar; `capacity.UNATTENDED_ENV` (`PROSPECTOR_UNATTENDED=<lane>`) carries
the mark into scripts a worker launches. Code that fans agents out to a thread
pool (`issue_triage/cross_lane.py`) submits through
`contextvars.copy_context().run` so the mark reaches the pool's threads.

Gated (unattended) work:

- verify worker: hunter security picks (`security_review.py --trigger
  autohunt`) and verify runs of hunter-queued requests;
- fix worker: hunted or automation-sourced `fix`, `resolve`, `describe`,
  objection continuations, and the resolve auto-review;
- issue-fix worker: hunter-queued solves and the public loop's agent steps
  (reply routing, follow-up revisions);
- the pipeline lane (sub-project 2).

Not gated: mechanical work (rebases, sandbox runs, ingests), and anything an
operator starts or queues (`source == "operator"` / Control-tab clicks / chat).

**Two layers.**

1. *Before claiming* — each worker asks `lane_health.capacity_open(lane)`
   (which calls `capacity.check`) once it holds an unattended item and before
   it claims it, so nothing is claimed and then stranded, and an idle machine
   never spends a probe. An item started under the cap runs to completion:
   its agents are not gated one by one, so no partial work (a fix authored,
   then refused review) is thrown away.
2. *At the agent call* — inside an unattended *batch* context (a process
   started with `PROSPECTOR_UNATTENDED`, which sub-project 2's pipeline lane
   sets), `run_agent` calls `capacity.check` before spawning and raises
   `CapacityPaused(decision)` instead of starting a new agent. Multi-agent
   phases therefore stop at the cap between agents.

A phase that meets `CapacityPaused` stops starting agents and exits as a
deferral, booking no failure.

### 4. Limit hits and lane health

`run_agent` separates a usage-limit rejection from an auth failure:

- **Limit hit** — the last `rate_limit_event` has `status == "rejected"`, or
  (fallback) the failure text matches `_LIMIT_SPENT`. Raises
  `CapacityExhausted(resets_at, window)`, a subclass of `AgentUnavailable` so
  existing broad handlers stay correct, and records `paused_until = resets_at`
  on `ai_capacity:<key>` with a `capacity:pause` ledger entry. Every machine on
  that account stops starting background agents; other accounts are
  unaffected. The workers' existing `AgentUnavailable` handlers end the item
  through their retryable path, and `lane_health.trip_agent_lanes` trips no
  lane for a spent limit — the pause holds the lanes until the reset. A
  weekly-limit rejection pauses until the weekly reset.
- **Auth failure** — unchanged: `AgentUnavailable`, lanes trip, retest.
- **Transient service errors** — a non-zero exit whose text matches
  `overloaded|529|429|rate_limit_error` without a usage-limit rejection raises
  `AgentTransient`; `lane_health.note_failure` books no failure for it, so it
  never counts toward a lane trip, and the item retries on its usual schedule.

Operator-started multi-agent jobs (analyze-clusters, issue-analyze, the
find-fixed passes) stop starting new agent batches after the first
`CapacityExhausted`, print one line — "AI usage limit reached — resets at
3:00 PM" — and exit non-zero, recording the pause as above.

### 5. Visibility and metering

- **Ledger.** Every agent run appends `agent:run` to the runs ledger: `lane`
  (or `operator`), `account` key, `unattended`, `model`, input/output/cache
  tokens, `cost_usd` (the result event's `total_cost_usd`), `started`,
  `finished`. A pause appends `capacity:pause` (`account`, `until`, `reason`).
- **API.** `GET /api/capacity` — per account: label, billing, plan, machines,
  policy (or defaults), latest reading and its age, the cap in effect now, the
  pacing line, the decision (allowed / paused + reason + retry), and today's
  background use by lane. `PUT /api/capacity/policy` — section 2.
- **Health strip.** While an account's background work is paused, one amber
  line per account: "Background AI paused · br…@gmail.com · 5-hour window 61%
  (day cap 50%) · resumes ~11:00 PM". It clears when work can start.
- **Control tab.** An **AI capacity** panel per account: the 5-hour bar with a
  marker at the cap in effect, the weekly bar with a marker at the pacing line,
  both resets, the decision, and today's background use by lane.
- **Machines.** Each worker's `worker_health:<id>` record carries its account
  key and label, so the roster shows which machines share an account.

## Interfaces

`pipeline/capacity.py` — the ONE capacity policy:

```python
@dataclass(frozen=True)
class Account: key: str; billing: Literal["subscription", "api"]; plan: str | None; label: str
@dataclass(frozen=True)
class Window: utilization: float; resets_at: datetime
@dataclass(frozen=True)
class Reading: five_hour: Window | None; seven_day: Window | None; status: str; at: datetime; by: str
@dataclass(frozen=True)
class Decision: allowed: bool; reason: str; retry_at: datetime | None
@dataclass(frozen=True)
class Policy: timezone: str; day_start: time; day_end: time; day_cap: float; night_cap: float
              weekly_pacing: bool; daily_budget_usd: float | None; saved: bool

def account() -> Account | None
def parse_rate_limit(event: dict, by: str) -> Reading | None
def record_reading(store, account: Account, reading: Reading) -> None
def record_pause(store, account: Account, until: datetime, reason: str) -> None
def policy(store, account: Account) -> Policy          # saved, else defaults
def check(store, account: Account | None, now: datetime | None = None) -> Decision
def unattended(lane: str) -> ContextManager[None]
def current_lane() -> str | None                      # ContextVar, else PROSPECTOR_UNATTENDED
class CapacityPaused(RuntimeError): decision: Decision
```

`pipeline/headless_agent.py` — `CapacityExhausted(AgentUnavailable)`,
`AgentTransient(RuntimeError)`, `parse_stream(…, on_rate_limit=None)`,
`run_agent` gating and recording.

`pipeline/store.py` — `load_ai_account`, `save_ai_account`, `load_capacity`,
`save_capacity_if_newer` (the conditional upsert), `capacity_spend(account,
since)` (sums `agent:run` cost).

## Testing

- Event parsing against streams captured from the real CLI: allowed, allowed
  with warning, rejected (5-hour and weekly), missing `unifiedWindows`.
- `check()` as a table: each rule; day/night boundaries in a non-UTC zone
  crossing midnight; pacing at the start, middle and end of a week; stale →
  probe; unknown → paused; API budget at, under and over.
- `save_capacity_if_newer` on SQLite and Postgres semantics: older loses, newer
  wins, pause and reading independent.
- `run_agent`: unattended + closed gate raises `CapacityPaused` without
  spawning; operator context never gated; a rejected stream raises
  `CapacityExhausted` and records the pause; transient text raises
  `AgentTransient`.
- Each worker: a `CapacityPaused`/`CapacityExhausted`/`AgentTransient` defers
  the item with the claim released, no failure booked, no lane trip.
- An operator-started multi-agent job stops at the first `CapacityExhausted`.
- Policy endpoint: edits only this machine's account; validation.
- Gates: `uv run pytest`, pyright 0, ruff 0, `pnpm run build` + eslint on
  touched files. A live check of the Control panel against real readings on a
  worktree backend with every lane off.

## Rollout

No PR/issue record shape changes, so no schema bump. New registry rows and
ledger phases are additive. `PROSPECTOR_UNATTENDED` is registered in
`settings_registry.INTERNAL`. On upgrade a subscription account runs on the
defaults (the operator's stated policy) until reviewed; an API-key account runs
no unattended agent work until a budget is set.
