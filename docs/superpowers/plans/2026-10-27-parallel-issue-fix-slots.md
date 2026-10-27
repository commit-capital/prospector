# Parallel Issue-Fix Slots Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one machine's issue-fix worker run up to `TRIAGE_ISSUE_FIX_SLOTS` requests at once, with no two slots or machines ever working the same request.

**Architecture:** The worker becomes one coordinator thread (reclaim, replies, follow-ups, triage, hunting) plus N slot threads that only claim and run, all in one process. Correctness rests on the existing compare-and-swap claim, a compare-and-swap `queue`, and hunting from the coordinator alone. Restarts requeue interrupted runs instead of failing them, and the verify scratch files slots share are written atomically.

**Tech Stack:** Python 3 (uv), pytest, SQLAlchemy store via `storekit`, TypeScript type in the frontend.

**Spec:** `docs/superpowers/specs/2026-10-26-parallel-issue-fix-slots-design.md`

## Global Constraints

- Every module starts with `from __future__ import annotations`; signatures typed precisely; imports qualified (AGENTS.md).
- Comments describe the code as it is now: no history, no contrast with alternatives.
- `TRIAGE_ISSUE_FIX_SLOTS`: int, group `machine`, default 1, editable, registered in `pipeline/settings_registry.py`.
- No store schema bump: the heartbeat keeps `current_issue` (the lowest issue in flight) and adds `current_issues`.
- No gate, prompt or `worker_health` change. `SHUTDOWN_TIMEOUT` stays 10 s.
- Gates before each commit: `uv run pytest <touched test files>`; before the PR: `uv run pytest`, `uv run ruff check .`, `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness`, and `cd prospector_app/frontend && pnpm run build`.

## Review Focus

1. A restart with several slots mid-run: every interrupted request comes back `queued`, none `failed`, attempts unchanged (Task 3 test with two claims).
2. An operator cancel or queue landing while a slot claims or the hunter queues: exactly one write wins, the other reports not done (Task 2).
3. Shutdown with slots mid-run: the heartbeat drops only after every slot and the coordinator have ended (Task 4).
4. Capacity closing partway through a fill pass: the pass stops queueing at once (Task 5).
5. Slots set above `verify_driver.large_slots()`: the worker logs one warning at start and still runs (Task 4).

Not a task: `verify_gc` keeps the pin plus the newest other generation, and `docker rmi` is un-forced so it refuses an image a running phase uses. A run survives one re-pin, as it does today with one slot. Slots don't change that exposure.

---

### Task 1: Write the shared verify scratch files atomically

**Files:**
- Modify: `pipeline/storekit.py:719-723` (`atomic_write`)
- Modify: `pipeline/verify_driver.py:752-766` (`write_suite_config`, `write_exclude_file`)
- Modify: `pipeline/prove.py:369,398,433,434` (baseline cache and exclude writes)
- Test: `pipeline/tests/test_storekit.py`

**Interfaces:**
- Produces: `storekit.atomic_write_text(path: Path, text: str) -> None`. It writes to a uniquely named temp file in `path.parent` (`tempfile.mkstemp(dir=..., prefix=path.name + ".")`), then `os.replace`s it, and removes the temp file on failure. `atomic_write` calls it.

- [ ] **Step 1: Write the failing tests** in `pipeline/tests/test_storekit.py`:
  - `test_atomic_write_text_replaces_whole_and_leaves_no_temp(tmp_path)`: write "a", then "b", to `tmp_path / "x.json"`. Assert the content is "b" and `list(tmp_path.iterdir()) == [tmp_path / "x.json"]`.
  - `test_concurrent_atomic_writes_use_distinct_temp_files(tmp_path, monkeypatch)`: wrap `os.replace` to record each `src`, and call `atomic_write_text` from two threads (a `threading.Barrier(2)` inside the wrapper holds both before replacing). Assert the two `src` paths differ and the final content equals one of the two payloads exactly.
- [ ] **Step 2:** Run `uv run pytest pipeline/tests/test_storekit.py -k atomic -v`. Expected: FAIL (`atomic_write_text` undefined).
- [ ] **Step 3:** Implement `atomic_write_text` and route `atomic_write` through it. Replace the six `write_text` calls listed above with `storekit.atomic_write_text(path, <same text>)`, keeping each file's text byte-identical (no indent or key-sorting change).
- [ ] **Step 4:** Run `uv run pytest pipeline/tests/test_storekit.py pipeline/tests/test_prove.py pipeline/tests/test_verify_driver.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `Write the verify scratch files slots share atomically`.

### Task 2: Make `fix_review.queue` a compare-and-swap

**Files:**
- Modify: `issue_triage/issue_model.py:272` (add `stage_fix_followup`)
- Modify: `issue_triage/fix_review.py` (`queue`)
- Test: `issue_triage/tests/test_fix_review.py`

**Interfaces:**
- Produces: `Issue.stage_fix_followup(section: dict) -> None` (the stage half of `record_fix_followup`, which then calls it). `queue` keeps its signature and `(ok, reason)` return. A lost race returns `(False, "another request landed first")`.

- [ ] **Step 1: Write the failing test** `test_two_queues_racing_on_one_issue_land_one(store, monkeypatch)`. Wrap `store.stamped_issue` so the first call, after reading, runs a nested `fix_review.queue(store, n, "solve", by="op")`. Then call `fix_review.queue(store, n, "solve", by="hunter", source="hunter")`. Assert the outer call returns `(False, "another request landed first")`, the stored request's `requested_by == "op"`, and the thread holds no entry from the hunter.
- [ ] **Step 2:** Run `uv run pytest issue_triage/tests/test_fix_review.py -k racing -v`. Expected: FAIL (the outer call overwrites).
- [ ] **Step 3:** Rewrite `queue` to read `store.stamped_issue(n)` and stage the request, the follow-up edit and the thread entry (`stage_fix_request`, `stage_fix_followup`, `stage_fix_thread`). It saves once with `store.save_issue_if(issue, stamp)` and returns the lost-race reason when that is False. Every existing refusal check stays in place, run against the stamped read.
- [ ] **Step 4:** Run `uv run pytest issue_triage/tests prospector_app/backend/tests/test_issue_fix_worker.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `Queue an issue-fix request under a compare-and-swap`.

### Task 3: Requeue a request a gone worker left running

**Files:**
- Modify: `prospector_app/backend/issue_fix_worker.py` (`recover_orphans`, `_reclaim`'s log line, the `FAILED_RETRY_COOLDOWN` comment, which no longer names a restart)
- Test: `prospector_app/backend/tests/test_issue_fix_worker.py:452-506`

**Interfaces:**
- Produces: `recover_orphans(store: IssueStore) -> list[int]`, same signature, returning the issues requeued. A requeued request is the claimed request with `status: "queued"`, its `host` and `started_at` removed, and `queued_at`, `attempts` and `source` unchanged. Its thread gets a `by: "worker"` note: `requeued: <who> mid-<action>`.

- [ ] **Step 1: Update the tests.** Rename `test_a_claim_this_host_made_before_the_process_started_ends_failed` to `..._is_requeued`. Assert `fix_review.fix_status(issue)[0] == "queued"`, `"host" not in issue.fix_request`, `issue.fix_request["attempts"] == 1`, and that the thread note starts `requeued:` and contains `restarted`. In the parametrized other-host test, expect `"queued"` where it expected `"failed"`. Add `test_a_restart_requeues_every_slot_s_claim_without_spending_an_attempt`: claim issues 1 and 2 on `studio` before `STARTED_AT`, with issue 2's request at `attempts: 2`. Assert `recover_orphans == [1, 2]`, both are `queued`, issue 2 keeps `attempts == 2`, and `issue_fix_worker.run_once(store)` then claims one of them.
- [ ] **Step 2:** Run `uv run pytest prospector_app/backend/tests/test_issue_fix_worker.py -k "claim or requeue" -v`. Expected: FAIL.
- [ ] **Step 3:** Change `recover_orphans` to stage the requeued request and the note, still under `save_issue_if`, and update its docstring and `_reclaim`'s message to "requeued".
- [ ] **Step 4:** Run `uv run pytest prospector_app/backend/tests/test_issue_fix_worker.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `Requeue the issue-fix requests a gone worker left running`.

### Task 4: Run N slots beside a coordinator

**Files:**
- Modify: `pipeline/settings.py` (add `issue_fix_slots`), `pipeline/settings_registry.py` (register beside `TRIAGE_SANDBOX_LARGE_SLOTS`)
- Modify: `prospector_app/backend/issue_fix_worker.py` (threads, `state`, `beat`, `_drain`)
- Modify: `prospector_app/backend/machines.py:41`, `pipeline/store.py:1440-1447` (docstring), `prospector_app/frontend/src/api.ts:141`
- Test: `prospector_app/backend/tests/test_issue_fix_worker.py`, `prospector_app/backend/tests/test_machines.py`

**Interfaces:**
- Produces:
  - `settings.issue_fix_slots() -> int`: `positive_int("TRIAGE_ISSUE_FIX_SLOTS", 1)`.
  - Registry: `_i("TRIAGE_ISSUE_FIX_SLOTS", "machine", "Issue-fix runs at once", "1", "How many issue-fix requests this machine carries out at once. Each needs a large sandbox slot for its suite runs.", maximum=8)`.
  - `issue_fix_worker.state["current"]: set[int]`, guarded by `_state_lock: threading.Lock`. `run_once` adds `n` on claim and discards it in `finally`.
  - `issue_fix_worker.in_flight() -> list[int]`: sorted copy of `state["current"]`.
  - `beat()` writes `current_issue: min(in_flight()) or None` and `current_issues: in_flight()`.
  - `_slot_loop() -> None`: while not `stop`, if schema, profile and `lane_health.open_or_retest(LANE)` allow, then `run_once`. Waits 1 s after a run, `POLL_SECONDS` otherwise, and prints exceptions.
  - `_drain()` keeps reclaim, replies, the ten-minute pass, triage and hunting on their timers every `POLL_SECONDS`, whether or not slots are busy, and no longer calls `run_once`. Its hunting call becomes `fill` in Task 5; until then it calls `hunt` when `in_flight()` is shorter than the slot count.
  - `startup()` starts the coordinator, `settings.issue_fix_slots()` threads named `issue-fix-slot-<i>`, and the beat thread. `_drained` is set once the coordinator and every slot thread have returned: the beat thread joins them, and the coordinator does not set it alone.
  - At startup, when `settings.issue_fix_slots() > verify_driver.large_slots()`, print one `[issue-fix] warning: …` line naming both numbers.
  - `machines.py` beat dict adds `"current_issues": rec.get("current_issues") or []`. In `api.ts`, `MachineBeat` gains `current_issues: number[]`.

- [ ] **Step 1: Write the failing tests:**
  - `test_racing_claims_on_one_request_land_once(store)`: queue a `solve` on 2. Eight threads behind a `Barrier(8)` call `store.claim_fix_request(2, host=f"s{i}")`. Assert exactly one non-None result and that the stored `host` equals its host.
  - `test_slots_run_every_request_once_and_no_issue_twice(store, monkeypatch)`: add issues 3–6, queue a `solve` on 1–6, and set `TRIAGE_ISSUE_FIX_SLOTS=3`. A fake `run_request` records `n`, sleeps 0.05 s, and returns `("done", "ok")`. Run three `_slot_loop` threads until every request is `done`, then set `stop`. Assert the recorded list is a permutation of 1–6 and that the peak `len(in_flight())` seen inside the fake was greater than 1.
  - `test_the_heartbeat_names_every_issue_in_flight(store, beats, monkeypatch)`: set `state["current"] = {4, 2}`. Assert the beat holds `current_issue == 2` and `current_issues == [2, 4]` (replaces `test_the_heartbeat_names_the_issue_in_flight_and_the_hunt`, keeping its `autohunt` assertion).
  - `test_the_heartbeat_outlives_a_slot_still_running(...)`: a slot thread blocked in a fake run. Set `stop`, and assert `_drained` is not set until the fake returns.
  - `test_more_slots_than_large_sandbox_slots_warns(monkeypatch, capsys)`: `large_slots` patched to 2, slots 4, thread targets patched to no-ops. Assert `startup()` prints one warning naming 4 and 2.
  - In `test_machines.py`, extend the existing beat test with `current_issues`.
- [ ] **Step 2:** Run `uv run pytest prospector_app/backend/tests/test_issue_fix_worker.py prospector_app/backend/tests/test_machines.py -v`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement the interfaces above. Update the module docstring's first paragraph ("One request at a time") to describe the slots and the coordinator.
- [ ] **Step 4:** Run `uv run pytest prospector_app/backend/tests pipeline/tests/test_settings_registry.py -v`, then `cd prospector_app/frontend && pnpm run build`. Expected: PASS, and tsc reports 0 errors.
- [ ] **Step 5: Commit** `Run issue-fix requests in parallel slots beside one coordinator`.

### Task 5: Keep idle slots fed within the daily budget

**Files:**
- Modify: `prospector_app/backend/issue_fix_worker.py` (`hunt`, `_hunted_today` callers, `_drain`)
- Test: `prospector_app/backend/tests/test_issue_fix_worker.py`

**Interfaces:**
- Consumes: `in_flight()`, `settings.issue_fix_slots()` (Task 4).
- Produces:
  - `fill(store: IssueStore, want: int) -> list[int]`. It queues up to `want` hunter `solve`s, never past the day's budget, in `fixability.hunt_key` order. The day's hunted count is read from the store, with `_hunted_today(store.issues_matching(("fix_request", "source"), ["hunter"]))`, not from the snapshot. The pass stops at the first closed `lane_health.capacity_open(LANE)` check. Returns the issues queued.
  - `hunt(store: IssueStore) -> int | None` becomes `fill(store, 1)`'s first issue or None. The existing hunter tests still pass.
  - `_drain` calls `triage` and then `fill(store, idle)`, where `idle = settings.issue_fix_slots() - len(in_flight()) - <queued solves this host may take>`, counting queued solves from `store.issues_matching(("fix_request", "status"), ["queued"])` filtered to `action == "solve"`. It skips the call when `idle <= 0`.

- [ ] **Step 1: Write the failing tests** (using the `hunting` fixture):
  - `test_a_fill_queues_one_per_idle_slot(hunting)`: `fill(hunting, 3) == [2, 1]`.
  - `test_a_fill_keeps_to_the_daily_budget_across_calls(hunting, monkeypatch)`: budget 1. Assert `fill(hunting, 3) == [2]`, then `fill(hunting, 3) == []`, with the `issue_data.issues` snapshot patched to a fixed stale copy taken before the first call.
  - `test_a_fill_stops_when_capacity_closes_midway(hunting, capacity, monkeypatch)`: wrap `fix_review.queue` to set `capacity["open"] = False` after its first success. Assert `fill(hunting, 3) == [2]`.
- [ ] **Step 2:** Run `uv run pytest prospector_app/backend/tests/test_issue_fix_worker.py -k "fill or hunter" -v`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement `fill`, re-express `hunt` through it, and wire `_drain`.
- [ ] **Step 4:** Run `uv run pytest prospector_app/backend/tests/test_issue_fix_worker.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `Keep the issue-fix slots fed within the hunter's daily budget`.

### Task 6: Full gates and PR

- [ ] **Step 1:** Run `uv run pytest`, `uv run ruff check .` and `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness`. Expected: all pass with zero findings.
- [ ] **Step 2:** Run the `architecture-review` skill on the branch. Then open the PR, whose description carries the rollout:
  1. Merge with the default of 1 slot.
  2. Set `TRIAGE_ISSUE_FIX_SLOTS=3` on the Studio and watch faults, run p50 and endings for a day.
  3. Run `colima stop && colima start --cpu 12 --memory 48`, then set 4.

  Success means `fixed` endings per day at 2–3× the Oct 20–26 baseline (3–6 a day), with no rise in machine faults.
