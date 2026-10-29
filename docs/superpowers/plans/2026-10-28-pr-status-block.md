# PR Status Block Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every open PR's page starts with a status block. It shows one of five states (Moving, Waiting, Ready, Needs you, Stalled), a sentence, a stage path and actions that fit the state. The PR Explorer's Status column shows the same state and sentence.

**Architecture:** `automation.classify` stays the one owner of a PR's standing. It gains a `state` for every bucket, and it reads an issue-fix PR's follow-up so our own PRs never get author-style answers. A new `pr_status` module adds what only the PR page needs: the stage path, what was tried, the agent seed, and the queue position and ETA. The queue position and ETA come from a separate endpoint, because ordering the hunter's pools costs about 1.6 s.

**Tech Stack:** Python 3 / FastAPI backend (`uv run pytest`, ruff, pyright); React + TypeScript frontend (pnpm, vitest, eslint).

**Spec:** `docs/superpowers/specs/2026-10-28-pr-status-block-design.md` (mockup: `docs/superpowers/specs/2026-10-28-pr-status-block-mockup.html`)

## Global Constraints

- The five states are exactly `moving | waiting | ready | needs-you | stalled`.
- One owner per decision. The state, the sentence (`reason`) and the owner come from `automation.classify`. The frontend never derives them.
- Status is derived on read. No new store table, section, registry key or schema bump.
- Workers decide nothing differently. No change to gates, retry bounds or follow-up budgets.
- The ETA is rough and qualified ("about N hours"). When it can't be measured, it's "unknown". Don't invent one.
- The Explorer costs no more per row than `classify` does today. Queue position and ETA appear on the PR page only.
- "Ask the agent why it's stalled" and "Abandon" are offered only in the Stalled state.
- Conventions (AGENTS.md):
  - every module starts with `from __future__ import annotations`;
  - every signature is precisely typed;
  - imports are qualified;
  - comments describe the present;
  - pnpm, never npm.

## Review Focus

1. **A bot PR whose issue isn't in the issue snapshot** (the issue was deleted, or ingest is behind). It falls back to the outsider classification and doesn't crash. Test in Task 2.
2. **A lane with no completed runs in the window.** Rate is 0 or missing, so the ETA is `None` and the page says "unknown". Test in Task 4.
3. **A Moving PR that's in no hunter pool** (it's waiting on CI, or on re-ingest). The next step is `None`, and the page shows the sentence with no position. Test in Task 4.
4. **A PR with no outcome rows.** `tried` is empty, and the seed still reads sensibly. Test in Task 3.
5. **Every `automation.OWNERS` bucket has a state.** A new bucket added later without one fails a test. Test in Task 1.

---

### Task 1: A state for every automation bucket

**Files:**
- Modify: `prospector_app/backend/automation.py` (`OWNERS`, `_r`, module docstring)
- Modify: `prospector_app/frontend/src/api.ts` (`AutomationStanding`, a new `AutomationState` type)
- Test: `prospector_app/backend/tests/test_automation.py`

**Interfaces:**
- Produces: `automation.STATES: dict[str, str]`, mapping bucket → state:
  - `merge-ready` → `ready`;
  - `approve-ci`, `rerun-ci`, `approve-parked`, `malicious` → `needs-you`;
  - worker-owned buckets → `moving`;
  - author-owned buckets → `waiting`;
  - the remaining you-owned handed buckets → `stalled`.
- Produces: every `classify` result carries `"state"`.
- Produces: in TS, `type AutomationState = "moving" | "waiting" | "ready" | "needs-you" | "stalled"`, and `AutomationStanding.state: AutomationState`.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_bucket_has_a_state():
    assert set(automation.STATES) == set(automation.OWNERS)
    assert set(automation.STATES.values()) <= {"moving", "waiting", "ready", "needs-you", "stalled"}

def test_a_merge_ready_pr_reads_ready_and_a_parked_change_needs_you():
    assert automation.STATES["merge-ready"] == "ready"
    assert automation.STATES["approve-parked"] == "needs-you"
    assert automation.STATES["needs-human"] == "stalled"
    assert automation.STATES["author-ci"] == "waiting"
    assert automation.STATES["hunt"] == "moving"
```

Also extend one existing `classify` test in the file so it asserts the result's `"state"`.

- [ ] **Step 2: Run** `uv run pytest prospector_app/backend/tests/test_automation.py -q`. Expected: FAIL, because `STATES` is undefined.
- [ ] **Step 3: Implement.** Add `STATES` next to `OWNERS`. `_r` adds `"state": STATES[bucket]`. Add the state to the docstring's account of the result, and add `state` to the TS types.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit:** `git commit -m "Give every automation bucket one of five states"`

### Task 2: Our own PRs read their issue's follow-up

**Files:**
- Modify: `pipeline/gates.py` (extract `opened_by_bot(pr: Pr) -> bool` from `backed`, and have `backed` call it)
- Modify: `prospector_app/backend/automation.py` (`classify` consults `_followup_standing` first for a bot PR; new buckets)
- Modify: `prospector_app/frontend/src/api.ts` (`AutomationOwner` gains `"maintainers"`)
- Test: `prospector_app/backend/tests/test_automation.py`

**Interfaces:**
- Consumes: `issue_triage.fix_review.open_pr(issue) -> int | None`, `fix_review.followup_for(fu, pr) -> dict`, `fix_review.status_line(issue) -> StatusLine` (its `text` is the sentence), and `prospector_app.backend.issue_data.issues() -> dict[int, Issue]`.
- Produces: `gates.opened_by_bot(pr: Pr) -> bool`.
- Produces: new buckets in `OWNERS` / `STATES` / `YOU_HANDED_BUCKETS`:

  | Bucket | Owner | State | Column |
  |---|---|---|---|
  | `handed-back` | you | `stalled` | `handed` |
  | `with-maintainers` | maintainers | `waiting` | `handed` |
  | `followup` | worker | `moving` | `auto` |

- Produces: `automation._followup_standing(pr: Pr) -> dict | None`. It finds the open issue whose `open_pr(issue) == pr.number`, scanning `issue_data.issues()` only when `gates.opened_by_bot(pr)`. It maps the follow-up state:
  - `handed-back` → `handed-back`;
  - `ready`, or an operator approval held (`fix_review.approval`) → `with-maintainers`;
  - any other open follow-up → `followup`.

  The reason is `fix_review.status_line(issue)["text"]`. It returns `None` when no issue matches, so the PR falls through to the outsider classification.

- [ ] **Step 1: Write the failing tests.** Build a bot-authored `Pr` (`author="app/" + bot login`) and a matching `Issue` with `fix_run.proposal.pr` and `fix_followup`, and monkeypatch `issue_data.issues`. Assert:
  - `test_a_handed_back_fix_pr_is_stalled_on_you`: bucket `handed-back`, state `stalled`, owner `you`, and the reason equals `fix_review.status_line(issue)["text"]`.
  - `test_a_green_fix_pr_waits_on_the_maintainers`: follow-up `ready` → bucket `with-maintainers`, state `waiting`.
  - `test_a_watching_fix_pr_is_moving`: bucket `followup`, state `moving`.
  - `test_a_fix_pr_whose_issue_is_missing_reads_as_any_pr` (Review Focus 1): an empty issue map gives the same result as the outsider path, with no exception.
  - `test_an_outsiders_pr_never_scans_issues`: a non-bot PR, with `issue_data.issues` monkeypatched to raise, still classifies.
- [ ] **Step 2: Run** `uv run pytest prospector_app/backend/tests/test_automation.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `opened_by_bot`, `_followup_standing`, and the new buckets. `classify` checks `_followup_standing` right after the malicious and stacked checks.
- [ ] **Step 4: Run** the Task 1–2 tests, `prospector_app/backend/tests/test_service_store.py`, and the frontend `homeCards.test.ts` (`pnpm exec vitest run src/views/homeCards.test.ts`). Expected: PASS. Update Home's card copy only if a test pins the old buckets of a bot PR.
- [ ] **Step 5: Commit:** `git commit -m "Classify Prospector's own fix PRs by their follow-up, not as an author's"`

### Task 3: The stage path, what was tried, and the agent seed

**Files:**
- Create: `prospector_app/backend/pr_status.py`
- Modify: `prospector_app/backend/app.py` (`GET /api/prs/{n}/status`)
- Modify: `prospector_app/frontend/src/api.ts` (`PRStatus` type, `api.prStatus(n)`)
- Test: `prospector_app/backend/tests/test_pr_status.py`

**Interfaces:**
- Consumes: `automation.classify` (Tasks 1–2), `pr_checks.checks_for_record(rec)["checks"]`, `pipeline.outcomes.read`, `data.runs()`, and `issue_data.runs()`.
- Produces:
  - `pr_status.STAGE_KEYS = ("analysis", "conflicts", "ci", "review", "design", "security", "verify", "ready")`.
  - `class Stage(TypedDict): key: str; label: str; status: Literal["done", "current", "failed", "todo"]; detail: str`
  - `stages(pr: Pr, standing: dict) -> list[Stage]`. The checks map onto the stage keys:

    | Stage | Read from |
    |---|---|
    | `analysis` | done when `freshness.is_current(pr, "analysis")` |
    | `conflicts` | `mergeable` and `drift` |
    | `ci` | `ci` |
    | `review` | `review` and `scans` |
    | `design` | `design` |
    | `security` | `security` |
    | `verify` | `verify` |

    The first stage that isn't done is `current`, or `failed` when its check failed. For a `handed-back` bucket, the stage named by the follow-up's `judged.by` is `failed`: CI → `ci`, a reviewer → `review`.
  - `tried(pr: Pr, limit: int = 5) -> list[dict]`. It returns the outcomes (`pipeline.outcomes.Outcome` plus `at`) whose subject is this PR, from `data.runs()`. For a bot PR it adds the matching issue's `issue-fix` / `issue-followup` outcomes from `issue_data.runs()`. Newest first, at most `limit`.
  - `seed(pr: Pr, standing: dict, tried: list[dict]) -> str`. This is the first message for the agent:
    - the PR number and title;
    - "ours, fixing issue #N" or "an outside contributor's";
    - the state and sentence;
    - one line per tried outcome (`lane stage fault: words`);
    - then this fixed ask: `"Work out why this PR is stuck and recommend one next step: re-attempt with specific notes, accept the blocker, or abandon. Say which and why. Don't take any action."`
  - `status(pr: Pr) -> dict`. It returns `{"standing": classify(pr), "stages": stages(...), "tried": tried(pr), "seed": seed(...) if standing["state"] == "stalled" else None, "issue": <bot PR's issue number> | None}`.
  - `GET /api/prs/{n}/status` returns `pr_status.status(rec)`, or 404 when the PR isn't in the store.

- [ ] **Step 1: Write the failing tests:**
  - `test_the_first_unfinished_stage_is_current`: CI passing and code review failing → `review` is `failed`, and everything after it is `todo`.
  - `test_a_handed_back_fix_pr_marks_its_blocker_failed`: `judged.by == "Greptile"` → `review` is `failed`.
  - `test_tried_lists_this_prs_outcomes_newest_first`: two PR-ledger rows with outcomes for #7 and one for #8 → two entries, newest first.
  - `test_the_seed_names_the_pr_its_blocker_and_the_ask`: the seed contains `"#7"`, the sentence, and the fixed ask.
  - `test_a_pr_with_nothing_tried_still_seeds` (Review Focus 4): `tried == []`, and the seed still contains the ask.
  - `test_the_seed_is_only_for_a_stalled_pr`: a moving PR → `status(...)["seed"] is None`.
- [ ] **Step 2: Run** `uv run pytest prospector_app/backend/tests/test_pr_status.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the module, the route and the TS type.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit:** `git commit -m "Derive a PR's stage path, what was tried and its agent seed"`

### Task 4: Queue position and ETA for a moving PR

**Files:**
- Modify: `prospector_app/backend/pr_status.py`
- Modify: `prospector_app/backend/app.py` (`GET /api/prs/{n}/status/next`)
- Modify: `prospector_app/frontend/src/api.ts` (`NextStep` type, `api.prNextStep(n)`)
- Test: `prospector_app/backend/tests/test_pr_status.py`

**Interfaces:**
- Consumes: `verify_worker.hunt_candidates(limit: int) -> dict[str, list[int]]` (lanes `security`, `verify`, `design`), `fix_worker.hunt_candidates(limit: int) -> list[tuple[str, int]]`, and `data.runs(since=...)`.
- Produces:
  - `LANE_PHASES = {"security": "security:review-one", "verify": "verify:single", "design": "design-review:run", "fix": "fix:single"}`.
  - `lane_rate(lane: str, *, days: int = 7) -> float | None`: completed runs of that lane's phase per hour over the window, or `None` when there were none.
  - `class NextStep(TypedDict): lane: str; position: int; of: int; eta_hours: float | None`
  - `next_step(n: int) -> NextStep | None`: the first lane whose pick order holds `n` (1-based `position`, `of` = the pool size), with `eta_hours = position / rate`. `None` when no pool holds the PR. Call each `hunt_candidates` once, with a limit large enough to cover every pool (`len(data.prs())`).
  - `GET /api/prs/{n}/status/next` returns `next_step(n)`.
- [ ] **Step 1: Write the failing tests** (monkeypatch the two `hunt_candidates` and `data.runs`):
  - `test_position_and_eta_come_from_the_pick_order_and_the_lane_rate`: the security order is `[3, 7, 9]` and 12 security runs ran in the last 24 h of a 1-day window → `NextStep(lane="security", position=2, of=3, eta_hours=4.0)`.
  - `test_a_lane_with_no_runs_has_no_eta` (Review Focus 2): `eta_hours is None`.
  - `test_a_pr_in_no_pool_has_no_next_step` (Review Focus 3): `next_step(42) is None`.
- [ ] **Step 2: Run** `uv run pytest prospector_app/backend/tests/test_pr_status.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement**, then time `next_step` against the configured store, read-only (`PGOPTIONS='-c default_transaction_read_only=on'`), and note the time for the PR description.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit:** `git commit -m "Say where a moving PR is in its lane's queue, and roughly when"`

### Task 5: The Explorer's Status column shows the state

**Files:**
- Modify: `prospector_app/frontend/src/components/prStatus.ts` (`stateLabel`, `stateTone`)
- Modify: `prospector_app/frontend/src/components/explorer/columns.tsx` (the `status` cell)
- Test: `prospector_app/frontend/src/components/prStatus.test.ts`

**Interfaces:**
- Consumes: `AutomationStanding.state` (Task 1).
- Produces:
  - `stateLabel(s: AutomationState): string`, one of `"Moving" | "Waiting" | "Ready" | "Needs you" | "Stalled"`.
  - `stateTone(s: AutomationState): "blue" | "yellow" | "green" | "orange" | "red"`, in the same order. These match the mockup's pills.
- [ ] **Step 1: Write the failing test.** `stateLabel` and `stateTone` return the values above for each of the five states.
- [ ] **Step 2: Run** `cd prospector_app/frontend && pnpm exec vitest run src/components/prStatus.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement.** The `status` cell renders a pill (`stateLabel`, with the `stateTone` class) before the existing `statusText` reason.
- [ ] **Step 4: Run** the test, then `pnpm run build` and `pnpm exec eslint <changed files>`. Expected: PASS, with no tsc errors.
- [ ] **Step 5: Commit:** `git commit -m "Show each PR's state in the Explorer's Status column"`

**Checkpoint:** Tasks 1–5 land as the first PR: the backend status and the Explorer column. Time `/api/prs/query` (the Explorer read) before and after on the configured store, and put both numbers in that PR.

### Task 6: The agent pane opens a new seeded conversation

**Files:**
- Modify: `prospector_app/frontend/src/components/AgentPane.tsx`

**Interfaces:**
- Produces: `useAgentPane().askFresh(question: string): void`. It opens the pane, starts a new session (the same as `createSession`), and sends `question` as its first message. The existing `askAbout` is unchanged.
- [ ] **Step 1: Implement.** `pending` becomes `{ text: string; fresh: boolean } | null`. The send effect runs `createSession()` first when `fresh`, and sends once the new session is active.
- [ ] **Step 2: Verify in the browser** (`preview_start` with the dev server). Call `askFresh("ping")` from the console hook or from Task 7's button. A new session appears under Recent, with "ping" as its first message. Run `pnpm run build` and eslint on the file.
- [ ] **Step 3: Commit:** `git commit -m "Let a page start a new agent conversation with a question"`

### Task 7: The status block on the PR page

**Files:**
- Create: `prospector_app/frontend/src/components/PRStatusBlock.tsx`
- Create: `prospector_app/frontend/src/components/prStatusActions.ts` (state → actions, pure)
- Modify: `prospector_app/frontend/src/views/PRDetail.tsx` (render the block above the Summary panel for an open PR)
- Test: `prospector_app/frontend/src/components/prStatusActions.test.ts`

**Interfaces:**
- Consumes: `api.prStatus(n)` (Task 3), `api.prNextStep(n)` (Task 4, fetched after the block renders, only when the state is `moving`), `askFresh` (Task 6), `stateLabel` / `stateTone` (Task 5), and `api.queueFix`, `api.issueFix`, `api.issueFixApprove`, plus the existing design-review / security-review job runners and `api.queueVerify`.
- Produces: `actionsFor(status: PRStatus, ours: boolean): StatusAction[]`, where `type StatusAction = "run-now" | "merge" | "quick-click" | "ask-agent" | "re-attempt" | "accept" | "abandon"`:
  - `moving` → `["run-now"]`, only when the next step names a lane with a manual trigger;
  - `waiting` → `[]`;
  - `ready` → `["merge"]`;
  - `needs-you` → `["quick-click"]`;
  - `stalled` → `["ask-agent", "re-attempt", "accept", "abandon"]`.
- Behavior of each action:
  - **re-attempt:** on our PR, `api.issueFix(issue, { action: "send-back", guidance })`; on an outsider's PR, `api.queueFix(n, "fix", guidance)`.
  - **accept:** on our PR, `api.issueFixApprove(issue, false)`; on an outsider's PR, it opens the existing merge control in `PRActionBar` with the override.
  - **abandon:** the existing close control in `PRActionBar`. On our PR that also ends
    the attempt: the issue hunter never re-picks an issue that has a `fix_run`
    (`issue_fix_worker._huntable`), and the follow-up records the PR `done`, closed.
  - **quick-click:** the bucket's existing action (approve CI, rerun CI, approve the parked change through `/api/prs/{n}/fix/approve`, or close for malicious).
  - The layout follows the mockup: pill and owner line, the sentence, the "tried" list (stalled only), the stage path, then the action buttons.
- [ ] **Step 1: Write the failing test.** `actionsFor` returns the lists above, and never includes `ask-agent` or `abandon` unless the state is `stalled`.
- [ ] **Step 2: Run** `pnpm exec vitest run src/components/prStatusActions.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement** `actionsFor`, the block, and the PRDetail wiring.
- [ ] **Step 4: Verify.** Run the tests, `pnpm run build`, and eslint on the changed files. In the browser, open one PR in each state, screenshot each block, and click "Ask the agent why it's stalled" on a stalled one: a new seeded conversation opens.
- [ ] **Step 5: Commit:** `git commit -m "Lead every PR page with its status, next step and fitting actions"`

**Checkpoint:** Tasks 6–7 land as the second PR, with the screenshots.
