# Health & queues PR 2 — pause, reorder, next picks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** An operator can pause a lane on one machine, move a queued item to the top of its queue, and see the hunters' next picks. All of it happens from the Lanes table.

**Architecture:**
- **Pause:** a per-host `lane_pause:<host>` registry row (`prospector_app/backend/lane_pause.py`). `lane_health.open_or_retest`, the one gate every pick path asks, answers False for a paused lane before it retests anything.
- **Priority:** an optional `priority: float` on `verify_request`, the PR `fix_request` and the issue `fix_request`. It is written only through compare-and-swap Store methods, so it can never undo a worker's claim. Every queue order sorts by it first. Absent reads as 0.
- **Next picks:** `verify_worker.hunt_candidates` and `fix_worker.hunt_candidates` rank the hunters' pools read-only. The overview carries them per lane, and it carries pauses as a new `paused` cell state.

**Spec:** `docs/superpowers/specs/2026-10-14-health-queues-lanes-design.md` (Operator pause, Queue order, Lanes → expanded row).

## Global Constraints

The constraints from the PR 1 plan carry over unchanged:
- `from __future__ import annotations`; precise types; pyright 0 errors; ruff clean.
- Qualified imports; present-tense comments.
- Store writes only through `Store`.
- No new environment variable. No schema bump: `priority` is an optional key that older code ignores.
- Frontend: `pnpm run build` passes; no new eslint errors in touched files; pnpm only.
- Never preview against the shared store.

## Review Focus

1. **A pause must stop approved pushes too**, not only new picks. `open_or_retest` False makes `pushes_open` False unless the lane is tripped on the agent alone, and a paused lane is not tripped. Test: `test_paused_lane_is_closed_and_books_nothing`.
2. **Move-to-top racing a worker's claim** must never flip a running request back to queued. Test: `test_prioritize_refuses_a_claimed_request`.
3. **A resumed lane** opens with no retest and no trip history. Test: `test_resume_reopens`.
4. **Queues where no item has a priority** keep exactly the present order. Tests: the existing `next_queued` / `_oldest` / `queued_issues` tests stay green, plus one ordering test per queue.
5. **Pausing one machine leaves other machines' lanes open.** Test: `test_pause_is_per_host`.

---

### Task 1: Lane pause

**Files:**
- Modify: `pipeline/store.py` (beside the machine-mode rows)
- Create: `prospector_app/backend/lane_pause.py`
- Modify: `prospector_app/backend/lane_health.py` (`open_or_retest`)
- Modify: `prospector_app/backend/app.py` (endpoints)
- Test: `pipeline/tests/test_store_machine_rows.py`, `prospector_app/backend/tests/test_lane_pause.py`

**Produces:**
- `Store.save_lane_pause(record: dict) -> None` — record `{host, lanes: {lane: {at, by}}}`
- `Store.load_lane_pauses() -> dict[str, dict]`
- `lane_pause.pause(host: str, lane: str, *, by: str) -> dict`
- `lane_pause.resume(host: str, lane: str) -> dict`
- `lane_pause.paused(host: str, lane: str, pauses: dict[str, dict] | None = None) -> bool`
- `lane_pause.paused_lanes(pauses: dict[str, dict]) -> dict[str, set[str]]`
- `POST /api/worker/lanes/{lane}/pause` and `POST /api/worker/lanes/{lane}/resume`, body `{host}`

The `lane` argument is a `worker_health.LANES` name; any other name raises `ValueError`, which the endpoints answer as 400.

- [ ] **Step 1: Failing tests**
  - Store round trip for the new row.
  - `test_pause_is_per_host`
  - `test_paused_lane_is_closed_and_books_nothing`: `open_or_retest` is False, and `worker_health` holds no failure for the lane.
  - `test_resume_reopens`
  - Unknown lane raises `ValueError`.
  - The endpoints, through `TestClient` with `TRIAGE_REPO` set.
- [ ] **Step 2: Run the tests and watch them fail**
- [ ] **Step 3: Implement**
  - `open_or_retest` first asks `lane_pause.paused(settings.worker_id(), lane)`. A failed read inside it falls through to the present behavior.
  - A pause writes the row whole. Each machine's pauses live in its own row, so two machines never race on one row.
- [ ] **Step 4: Run the tests and watch them pass; commit**

### Task 2: Queue priority and move-to-top

**Files:**
- Modify:
  - `pipeline/wire.py` (`VerifyRequest.priority`)
  - `pipeline/store.py` (`prioritize_verify_request`, `prioritize_fix_request`)
  - `issue_triage/issue_store.py` (`prioritize_fix_request`)
  - `verify_worker.next_queued`
  - `fix_worker._oldest`
  - `issue_triage/fix_review.queued_issues`
  - `verify_queue.queue_entries` / `fix_queue.queue_entries` (order)
  - `app.py`
- Create: `prospector_app/backend/queue_order.py` (move-to-top: reads the queue, sets the top priority)
- Test: `pipeline/tests/test_store_machine_rows.py`, `prospector_app/backend/tests/test_queue_order.py`

**Produces:**
- `Store.prioritize_verify_request(n: int, priority: float) -> bool` — CAS on `saved_at`; only for status `queued` or `waiting-for-base`
- `Store.prioritize_fix_request(n: int, priority: float) -> bool` — CAS; only for `queued`
- `IssueStore.prioritize_fix_request(n: int, priority: float) -> bool` — CAS; only for `queued`
- `queue_order.top(kind: Literal["verify", "fix", "issue-fix"], n: int) -> dict` — returns `{kind, n, priority}`; raises `ValueError` when the item is not waiting
- `POST /api/verify/queue/{n}/top`, `POST /api/fix/queue/{n}/top`, `POST /api/issues/{n}/fix/top`

Priority is one more than the highest priority in that queue, so repeated clicks stack in click order.

- [ ] **Step 1: Failing tests**
  - `test_prioritize_refuses_a_claimed_request` (claim lands between read and write).
  - Each of the three orders takes a prioritized item first.
  - Equal-priority items keep the present order.
  - `top` stacks: the second click beats the first.
  - `top` on a running item raises.
- [ ] **Step 2: Run the tests and watch them fail**
- [ ] **Step 3: Implement**
  - Each order's sort key gains a leading `-float(req.get("priority") or 0)`.
  - The claim copies no `priority` forward: a running request is out of the order.
- [ ] **Step 4: Run the tests and watch them pass; run the backend and issue_triage suites; commit**

### Task 3: Overview — paused cells and the hunters' next picks

**Files:**
- Modify: `control_overview.py`, `verify_worker.py`, `fix_worker.py`
- Test: `test_control_overview.py`, `prospector_app/backend/tests/test_hunt_candidates.py`

**Produces:**
- `verify_worker.hunt_candidates(limit: int = 5) -> dict[str, list[int]]` — keys `security`, `verify`, `design`; each in `hunt_key` order
- `fix_worker.hunt_candidates(limit: int = 5) -> list[tuple[str, int]]` — `(action, n)` in `_hunt_key` order, across actions
- `CellState` gains `"paused"`. Precedence: stood down, offline, off, paused, tripped, AI paused, on.
- `Inputs.paused: dict[str, set[str]]` (host → health lanes)
- `Inputs.picks: dict[str, list[Pick]]`
- `LaneRow.picks: list[Pick]`, where `Pick = {pr: int, title: str | None, action: str | None}`
- `QueueItem.priority: float | None`

`verify_worker.next_auto` and `hunt_candidates` build their pools through one shared helper, so the next picks are exactly the pools the hunter picks from.

- [ ] **Step 1: Failing tests**
  - A paused lane reads `paused`, is not in Needs you, and does not count as on in the status line.
  - `picks` pass through to their lane.
  - `hunt_candidates` orders by `hunt_key` and caps at `limit`.
- [ ] **Step 2: Run the tests and watch them fail**
- [ ] **Step 3: Implement; `gather` wraps each picks read in `_safe`**
- [ ] **Step 4: Run the tests and watch them pass; commit**

### Task 4: Frontend controls

**Files:**
- Modify:
  - `api.ts` (types: `paused`, `picks`, `priority`; calls: `pauseLane`, `resumeLane`, `topVerify`, `topFix`, `topIssueFix`)
  - `control/overviewText.ts` (+test: `paused` label; `nextText` counts a paused-only lane as not running)
  - `control/Lanes.tsx`
- Test: `control/overviewText.test.ts`

Lanes row:
- A pause/resume button for this machine's cell (`overview.local`), in the last column. It is shown only when this machine runs the lane.
- The expanded row lists:
  - each machine's pause, with Resume;
  - the queue in worker order, with **Move to top** and **Remove**;
  - the hunter's next picks, read-only;
  - recent endings.
- Autofix keeps `FixTable` below the ordered queue for parked changes.

- [ ] **Step 1: Failing TS tests**
- [ ] **Step 2: Implement; `pnpm run build`, `pnpm test`, eslint on touched files**
- [ ] **Step 3: Commit**

### Task 5: Verify and ship

- [ ] Full gates: `uv run pytest`, pyright, ruff, frontend build.
- [ ] Preview on a scratch store: workers off, no bot or push key, a backup of `pipeline/store/store.db` taken first and restored after. Check:
  - Pause then resume this machine's Verify lane.
  - A seeded queue: move the last item to the top, and the order changes.
  - Next picks render.
- [ ] Whole-branch review on Opus; one fix pass; open the PR.
