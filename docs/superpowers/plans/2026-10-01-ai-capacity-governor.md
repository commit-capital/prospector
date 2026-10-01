# AI Capacity Governor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unattended agent work spends only the AI capacity each account's owner grants (time-of-day caps on the 5-hour window plus weekly pacing for a subscription; a daily budget for an API key), set per account in the Setup tab and visible in the app.

**Architecture:** `pipeline/capacity.py` is the one capacity policy: it identifies the machine's AI account (`claude auth status --json`), parses the CLI's `rate_limit_event` readings, keeps the newest per-account reading and any pause in the shared store, and answers `check()`. `headless_agent.run_agent` records every run's reading and usage, classifies limit hits and transient errors, and refuses to start an agent in an unattended batch context when the gate is closed. Workers check the gate before claiming unattended items; `lane_health` stops treating limit hits and transient errors as faults. A backend view module serves `/api/capacity`; the frontend shows a Setup card, a Control panel and a health-strip line.

**Tech Stack:** Python 3.14 (uv), SQLAlchemy over SQLite/Postgres, FastAPI, React + TypeScript (pnpm, Vite).

**Spec:** `docs/specs/2026-10-01-ai-capacity-governor-design.md`

## Global Constraints

- Defaults: `day_start 08:00`, `day_end 23:00`, `day_cap 0.50`, `night_cap 0.90`, `weekly_pacing true`, `WEEKLY_SLACK 0.05`, `READING_MAX_AGE 10 minutes`, transient deferral 5 minutes.
- An API-key account with no `daily_budget_usd` runs no unattended agent work.
- Operator-started work (Control-tab clicks, chat, `source == "operator"` requests) is never gated.
- The raw account email is never stored; the label is masked (`br…@gmail.com · Max`).
- Unknown (no account / no reading / no `unifiedWindows`) ⇒ background agent work paused.
- Repo conventions (CLAUDE.md): comments describe the code as it is now; precise annotations; qualified imports; every env var registered in `pipeline/settings_registry.py`; pyright 0, ruff 0, frontend `pnpm run build` + eslint on touched files.
- **Spec revision (worker granularity):** a worker item started under the cap runs to completion; workers gate before claiming. The at-agent-call refusal applies inside an unattended *batch* context (`PROSPECTOR_UNATTENDED`), which sub-project 2's pipeline lane sets. Update the spec's "Two layers" paragraph to say so.

## Review Focus

1. Day window crossing midnight or a policy whose `day_start > day_end` (e.g. 22:00–06:00) — cap must follow the policy's zone, not UTC or the server's zone.
2. Two machines on one account writing readings out of order — an older reading must never replace a newer one (conditional upsert).
3. A CLI stream with no `rate_limit_event` (older CLI, API key auth) — no crash, no reading recorded, gate reads "unknown" only when a decision is needed.
4. A limit hit inside a worker item — no lane trip, a pause recorded, the item ends retryable.
5. `claude auth status` missing/failing/slow — `account()` returns None within a timeout; never blocks a worker loop indefinitely.

---

### Task 1: Store accessors for accounts, capacity rows and spend

**Files:**
- Modify: `pipeline/store.py` (registry section near `_load_registry`/`_save_registry`, ~line 673)
- Test: `pipeline/tests/test_capacity_store.py`

**Interfaces:**
- Produces:
  - `Store.load_ai_account(key: str) -> dict | None` / `Store.save_ai_account(key: str, data: dict) -> None` — registry `ai_account:<key>`.
  - `Store.load_capacity(key: str) -> dict` — registry `ai_capacity:<key>`, `{}` when absent.
  - `Store.save_capacity_reading_if_newer(key: str, reading: dict) -> None` — atomic upsert of `{"reading": …}` that replaces the stored reading only when `reading["at"]` (ISO UTC) is newer; keeps `pause`.
  - `Store.save_capacity_pause_if_newer(key: str, pause: dict) -> None` — same for `{"pause": {"until", "reason", "at"}}`, keeping `reading`.
  - `Store.capacity_spend(account: str, since: str) -> float` — sum of `cost_usd` over `agent:run` ledger records with that `account`, `unattended` true, `ts >= since`.

Two independent rows would make the halves trivially independent, but the spec keeps one `ai_capacity:<key>` row; implement each half's conditional write as a read-merge-write inside one transaction with `SELECT … FOR UPDATE` on Postgres (SQLite serializes writers), comparing the half's `at` before merging.

- [ ] **Step 1: Write failing tests** — older reading loses, newer wins; pause write keeps reading and vice versa; `load_capacity` on a missing key is `{}`; `capacity_spend` sums only matching unattended records since the bound.

```python
def test_older_reading_never_replaces_a_newer_one(store):
    store.save_capacity_reading_if_newer("k", {"at": "2026-10-01T10:00:00+00:00", "five_hour": {"utilization": 0.4}})
    store.save_capacity_reading_if_newer("k", {"at": "2026-10-01T09:59:00+00:00", "five_hour": {"utilization": 0.9}})
    assert store.load_capacity("k")["reading"]["five_hour"]["utilization"] == 0.4

def test_a_pause_and_a_reading_are_kept_independently(store):
    store.save_capacity_reading_if_newer("k", {"at": "2026-10-01T10:00:00+00:00"})
    store.save_capacity_pause_if_newer("k", {"at": "2026-10-01T10:01:00+00:00", "until": "2026-10-01T15:00:00+00:00", "reason": "limit"})
    row = store.load_capacity("k")
    assert row["reading"]["at"] == "2026-10-01T10:00:00+00:00" and row["pause"]["reason"] == "limit"

def test_capacity_spend_sums_unattended_runs_of_one_account(store):
    for acct, unattended, cost in (("k", True, 1.5), ("k", False, 9.0), ("other", True, 4.0), ("k", True, 0.5)):
        store.append_run({"phase": "agent:run", "account": acct, "unattended": unattended, "cost_usd": cost,
                          "started": "2026-10-01T10:00:00+00:00", "finished": "2026-10-01T10:01:00+00:00"})
    assert store.capacity_spend("k", "2026-10-01T00:00:00+00:00") == 2.0
```

- [ ] **Step 2:** Run `uv run pytest pipeline/tests/test_capacity_store.py -q` — FAIL (no such methods).
- [ ] **Step 3:** Implement in `pipeline/store.py`. `capacity_spend` filters on `runs.kind == "pr"`, `data["phase"].as_string() == "agent:run"`, `data["account"].as_string() == account`, `ts >= since` and sums in Python (`unattended` truthy). Confirm `storekit.parse_run` accepts the `agent:run` record (a `PhaseRun` with extra keys); if it validates keys strictly, extend it.
- [ ] **Step 4:** Tests pass; `uv run pyright pipeline/store.py` 0.
- [ ] **Step 5:** Commit `Store: AI account policy, capacity readings/pauses, and background spend`.

### Task 2: `pipeline/capacity.py` — accounts, readings, policy, `check()`

**Files:**
- Create: `pipeline/capacity.py`
- Modify: `pipeline/settings_registry.py` (INTERNAL += `"PROSPECTOR_UNATTENDED"`)
- Test: `pipeline/tests/test_capacity.py`

**Interfaces:**
- Consumes: Task 1 store methods.
- Produces (exact):

```python
READING_MAX_AGE = timedelta(minutes=10)
WEEKLY_SLACK = 0.05
UNATTENDED_ENV = "PROSPECTOR_UNATTENDED"

@dataclass(frozen=True)
class Account:
    key: str
    billing: Literal["subscription", "api"]
    plan: str | None
    label: str

@dataclass(frozen=True)
class Window:
    utilization: float
    resets_at: datetime

@dataclass(frozen=True)
class Reading:
    five_hour: Window | None
    seven_day: Window | None
    status: str
    at: datetime
    by: str

@dataclass(frozen=True)
class Policy:
    timezone: str
    day_start: time
    day_end: time
    day_cap: float
    night_cap: float
    weekly_pacing: bool
    daily_budget_usd: float | None
    saved: bool

@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    retry_at: datetime | None

class CapacityPaused(RuntimeError):
    decision: Decision

def account(refresh: bool = False) -> Account | None            # cached per process; `claude auth status --json`, 15s timeout
def account_from_status(status: dict, worker_id: str) -> Account | None
def parse_rate_limit(event: dict, by: str, at: datetime) -> Reading | None
def reading_to_dict(r: Reading) -> dict / reading_from_dict(d: dict) -> Reading | None
def record_reading(store: Store, acct: Account, reading: Reading) -> None
def record_pause(store: Store, acct: Account, until: datetime, reason: str) -> None
def default_policy(billing: str) -> Policy
def policy(store: Store, acct: Account) -> Policy
def validate_policy(raw: dict, billing: str) -> Policy             # ValueError on bad input
def policy_to_dict(p: Policy) -> dict
def cap_now(p: Policy, now: datetime) -> tuple[float, datetime]     # (cap in effect, next boundary)
def pacing_line(seven_day: Window, now: datetime) -> float          # elapsed share of the week + slack
def check(store: Store, acct: Account | None, now: datetime | None = None,
          probe: Callable[[], Reading | None] | None = None) -> Decision
def unattended(lane: str) -> AbstractContextManager[None]
def current_lane() -> str | None                                    # ContextVar, else env PROSPECTOR_UNATTENDED
```

`check` rules in order (spec §3): pause in the future → paused (retry = pause until); stale or missing reading → call `probe()` (the caller passes one that runs `headless_agent.probe_reading`; tests pass a fake) and record its reading; still none or no `five_hour` for a subscription → paused "no capacity reading" retry now+10m; subscription: `five_hour.utilization >= cap` → paused, retry = min(`five_hour.resets_at`, next policy boundary); pacing on and `seven_day.utilization > pacing_line` → paused, retry = week_start + (utilization − slack)·7d; API: `daily_budget_usd is None` → paused "set a daily budget"; spend since local midnight ≥ budget → paused until next local midnight; else allowed.

`cap_now`: convert `now` to `ZoneInfo(p.timezone)`; daytime is `day_start <= t < day_end` when `day_start < day_end`, else `t >= day_start or t < day_end`; next boundary is the next of `day_start`/`day_end` after `now` in that zone, returned in UTC.

`account_from_status`: `loggedIn` false ⇒ None; key = sha256(`f"{authMethod}|{orgId}|{email}"`)[:16] when orgId or email, else sha256(`f"machine|{worker_id}"`)[:16]; billing `subscription` iff `authMethod == "claude.ai"`; label = masked email (`email[:2] + "…@" + domain`) or `"API key"` plus `" · " + plan.title()` when plan.

- [ ] **Step 1: Failing tests** (table-driven):
  - `account_from_status` for a Max login, an API-key login (no email), logged-out ⇒ None; label masks the email.
  - `parse_rate_limit` on the captured event (`{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1790895600,"rateLimitType":"five_hour","unifiedWindows":{"five_hour":{"utilization":0.61,"resetsAt":1790895600},"seven_day":{"utilization":0.18,"resetsAt":1791439200}}}}`), a rejected one, and one without `unifiedWindows` (windows None, status kept).
  - `cap_now` at 07:59, 08:00, 22:59, 23:00 America/Los_Angeles (UTC `now`), and a 22:00–06:00 policy across midnight.
  - `pacing_line` at week start (≈0.05), mid-week (≈0.55), end (≈1.05).
  - `check`: each rule allowed/paused with the expected `retry_at`; stale reading triggers the injected probe exactly once; probe returning None ⇒ paused unknown; account None ⇒ paused unknown; API with no budget ⇒ paused; API spend under/over budget.
  - `validate_policy` rejects `day_cap` 0 or > 1, bad times, unknown zone, negative budget.
  - `unattended("pipeline")` sets `current_lane()`; env var fallback; context reset on exit.
- [ ] **Step 2:** Run — FAIL (module missing).
- [ ] **Step 3:** Implement `pipeline/capacity.py` (module docstring states it is the ONE capacity policy). `account()` caches in a module global guarded by a lock; runs `[headless_agent.CLAUDE_BIN, "auth", "status", "--json"]` with `timeout=15`; any failure ⇒ None (not cached, so a later call retries).
- [ ] **Step 4:** Tests pass; pyright/ruff clean; `uv run pytest pipeline/tests/test_settings_registry.py -q` passes.
- [ ] **Step 5:** Commit `Add the AI capacity policy: accounts, readings, caps and weekly pacing`.

### Task 3: `headless_agent` — record readings and usage, classify limits, unattended refusal

**Files:**
- Modify: `pipeline/headless_agent.py` (`parse_stream`, `run_agent`, `probe`, new exceptions)
- Test: `pipeline/tests/test_headless_agent.py`

**Interfaces:**
- Consumes: Task 2 (`capacity.account`, `parse_rate_limit`, `record_reading`, `record_pause`, `check`, `current_lane`, `CapacityPaused`).
- Produces:
  - `class CapacityExhausted(AgentUnavailable)` with `.resets_at: datetime | None`, `.window: str | None`.
  - `class AgentTransient(RuntimeError)`.
  - `parse_stream(lines, on_event=None, on_result=None, on_raw=None, on_rate_limit=None)` — calls `on_rate_limit(event)` for each `rate_limit_event`.
  - `probe_reading(timeout: int = 180) -> capacity.Reading | None` — the probe's reading (records it via `run_agent`).
  - `limit_spent(reason: str) -> bool`, `transient(reason: str) -> bool` — the regex tests other modules use.

Behavior:
- Before spawning: `lane = capacity.current_lane()`; if set, `acct = capacity.account()`, `d = capacity.check(store, acct, probe=probe_reading)`; if not allowed raise `capacity.CapacityPaused(d)`. (Avoid recursion: `probe_reading` runs with the lane cleared.)
- After the run (success or failure), in a `try/except Exception: pass` block so bookkeeping never fails a run: if a rate-limit event arrived, `record_reading`; if the result event has `usage`, append an `agent:run` ledger record `{phase, lane: lane or "operator", account, unattended: lane is not None, model, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, cost_usd: total_cost_usd, started, finished}` via `Store().append_run`. Skip both when there is neither (tests' fake streams).
- On non-zero exit: if the last rate-limit event has `status == "rejected"` or `limit_spent(failure)`: `record_pause(…, until=resets_at or now+1h, reason)` and raise `CapacityExhausted`; elif auth `unavailable_reason` ⇒ `AgentUnavailable`; elif declined ⇒ `AgentDeclined`; elif `transient(failure)` ⇒ `AgentTransient`; else `RuntimeError` as today.
- `_TRANSIENT = re.compile(r"overloaded|\b529\b|\b429\b|rate_limit_error", re.I)` (only consulted after the limit check).

- [ ] **Step 1: Failing tests** using the existing `_FakeProc` pattern: a stream with a `rate_limit_event` records a reading (patch `capacity.account` → fixed Account and the store to a temp `Store(tmp_path)`); a result event with usage appends one `agent:run` record; a non-zero exit with a rejected event raises `CapacityExhausted` with the event's reset and records a pause; "Claude AI usage limit reached" text alone raises `CapacityExhausted`; "Not logged in" still raises plain `AgentUnavailable` (not `CapacityExhausted`); "API Error: 529 overloaded" raises `AgentTransient`; with `capacity.unattended("pipeline")` and a closed gate `run_agent` raises `CapacityPaused` and never calls Popen; with no lane, a closed gate is ignored.
- [ ] **Step 2–4:** FAIL → implement → PASS; run the whole `pipeline/tests/test_headless_agent.py`.
- [ ] **Step 5:** Commit `Record capacity readings and usage from every agent run; tell limit hits from outages`.

### Task 4: Lane health and heartbeats know about capacity

**Files:**
- Modify: `prospector_app/backend/lane_health.py` (`trip_agent_lanes`, `note_failure`), the three workers' heartbeat writers to include `ai_account` (`key`, `label`, `billing`) — find where each worker's health record is built (`worker_health.record_*`/`save_worker_health`).
- Test: `prospector_app/backend/tests/test_lane_health.py` (create if absent; else extend the existing lane-health tests)

**Interfaces:**
- Consumes: `headless_agent.limit_spent`, `headless_agent.transient`, `capacity.account`.
- Produces: `trip_agent_lanes(reason)` returns without tripping when `limit_spent(reason)`; `note_failure(lane, kind=…, reason=…)` returns without booking when `transient(reason)`; worker heartbeats carry `ai_account`.

- [ ] Steps: failing tests (a limit reason trips nothing; an auth reason still trips; a transient failure books nothing; an ordinary failure still books) → implement → pass → commit `Lane health: a spent usage limit is a pause, a service overload is not a fault`.

### Task 5: Workers gate unattended items before claiming

**Files (one subtask per worker, independent):**
- 5a `prospector_app/backend/verify_worker.py` — `next_queued(skip_auto: bool = False)` skips `AUTO_REQUEST_SOURCES` requests when `skip_auto`; the drain loop computes `gate = capacity.check(...)` once per tick (helper `_capacity_open() -> bool` that logs the reason once per change) and passes `skip_auto=not gate`, and calls `next_auto` only when open. Tests in `prospector_app/backend/tests/test_verify_worker*.py` style.
- 5b `prospector_app/backend/fix_worker.py` — the queued-request selection (the ranking at ~line 402) skips requests with `source in ("auto", "objection")` whose action is agentic (`fix`, `resolve`, `describe`) when the gate is closed; mechanical `update`/`rebase` are not gated; the parked-resolve auto-review and the agentic `fix` hunt (`TRIAGE_FIX_HUNT_FIX`) run only when open.
- 5c `prospector_app/backend/issue_fix_worker.py` — the claim step skips requests whose `source` is not an operator source (`hunter`, `public`, `followup`) when closed; `hunt()` and `public_loop.answer_replies` run only when open.

**Interfaces:**
- Consumes: `capacity.account()`, `capacity.check(store, acct, probe=headless_agent.probe_reading)` → `Decision`.
- Produces: `_capacity_open()` per worker (module-private), logging `[<lane>] background AI paused: <reason> (retry ~<time>)` once per state change.

- [ ] Each subtask: failing test (closed gate ⇒ auto item not picked, operator item picked; open gate ⇒ both) → implement → pass → commit `<worker>: hold unattended agent work while the capacity gate is closed`.

### Task 6: Operator-run multi-agent jobs stop at the first limit hit

**Files:** `pipeline/analyze_clusters.py`, `issue_triage/analyze_issues.py`, `issue_triage/find_fixed.py`, `alert_triage/find_fixed.py`, `alert_triage/advisory_find_fixed.py` (each has a `ThreadPoolExecutor` wave over `as_completed`).

Behavior: on `headless_agent.CapacityExhausted` from a batch future, cancel every not-yet-started future, skip retry passes, print `"AI usage limit reached — resets at <local time>; stopping. <n> batch(es) not started."`, and exit 1. A `capacity.CapacityPaused` (unattended batch context) is handled the same way with its decision's reason and retry time, exiting 0 (a deferral, not a failure).

- [ ] Steps: failing test per script with a fake `run_batch_agent` raising `CapacityExhausted` on the first batch ⇒ later batches never run, message printed, rc 1 → implement → pass → commit `Stop multi-agent jobs at the first usage-limit hit`.

### Task 7: Backend capacity view and API

**Files:**
- Create: `prospector_app/backend/capacity_view.py`
- Modify: `prospector_app/backend/app.py` (routes), `prospector_app/backend/system_health.py` (health-strip items)
- Test: `prospector_app/backend/tests/test_capacity_api.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces:
  - `capacity_view.accounts() -> list[AccountView]` — every account seen in any worker's heartbeat plus this machine's: `{key, label, billing, plan, machines: list[str], this_machine: bool, policy: dict, policy_saved: bool, reading: dict | None, reading_age_seconds: float | None, cap_now: float | None, next_boundary: str | None, pacing_line: float | None, decision: {allowed, reason, retry_at}, spend_today_by_lane: dict[str, float], weekly_resets_at: str | None}`. `decision` uses `check(..., probe=None)` — the view never spends capacity to refresh.
  - `GET /api/capacity` → `{"accounts": [...]}`.
  - `PUT /api/capacity/policy` body = policy fields → validates with `capacity.validate_policy` against *this machine's* account, saves `ai_account:<key>`, returns the account view; 409 when this machine has no detectable account; 400 on invalid input.
  - `system_health` adds one `HealthItem` per paused account that has a worker online: `kind="capacity"`, `severity="warning"`, label as in the spec.

- [ ] Steps: failing tests (GET shape with a seeded reading/policy; PUT saves and validates; PUT refuses another account's key — the body carries no key, only this machine's account is written; health item appears when paused and not when allowed) → implement → pass → commit `Serve each AI account's capacity and policy, and flag paused background work`.

### Task 8: Frontend — Setup card, Control panel, health strip

**Files:**
- Modify: `prospector_app/frontend/src/api.ts` (types + `capacity()`, `saveCapacityPolicy()`), `prospector_app/frontend/src/views/Setup.tsx` (AI capacity card in the worker section), `prospector_app/frontend/src/views/ControlPanel.tsx` (AI capacity panel under Worker health), health strip component (renders `kind="capacity"` items with the amber tone; find the component consuming `/api/system-health`), `prospector_app/frontend/src/styles.css`.

**Interfaces:**
- Consumes: Task 7 JSON.

Card (this machine's account): label, "shared with <machines>", fields (zone select of `Intl.supportedValuesOf("timeZone")`, `day_start`, `day_end`, day/night caps as % inputs, weekly pacing toggle; for API: daily budget $), weekly reset read-only, "using defaults — review" note when `!policy_saved`, Save → PUT, inline validation error. Panel: per account a 5-hour bar with a cap marker, a weekly bar with a pacing marker, resets, decision line, today's spend by lane.

- [ ] Steps: implement → `pnpm run build` (0 TS errors) → `pnpm exec eslint` on touched files (no new errors) → live check in the browser pane against a worktree backend with all lanes off → commit `Show and set each AI account's capacity in the Setup and Control tabs`.

### Task 9: Spec revision, full gates, PR

- [ ] Update the spec's "Two layers" paragraph per the Global Constraints revision.
- [ ] `uv run pytest -q`, `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness`, `uv run ruff check .`, frontend build + eslint.
- [ ] Whole-branch review by a fresh reviewer; fix Critical/Important findings.
- [ ] Commit, push, open the PR (Problem / Change / Test plan), bind it, report CI.
