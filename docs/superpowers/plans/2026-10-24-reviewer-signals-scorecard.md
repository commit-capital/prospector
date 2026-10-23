# Reviewer signals and scorecard (ratchet phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The app suggests which recorded reviewer verdicts were probably wrong, from outcomes it already stores; the Policy page shows a queue of those to label and a scorecard per reviewer; and old captured inputs are cleared.

**Architecture:** One backend module, `prospector_app/backend/reviewer_ratchet.py`, derives everything on read, the way `trust_ladder.py` does.
- `gather(since)` makes a fixed number of bulk reads: verdict rows, PR rows, issue rows, activity for those PRs, and `fix:single` ledger rows.
- `signals(facts)` is a pure function that turns those reads into suggestions.
- `scorecard(rows)` is a pure function over labeled rows.

One route serves both, and one section on the Policy page shows them. Labeling still happens on the subject's page through phase 2's panel; the queue links there.

**Tech Stack:** Python 3, SQLAlchemy, FastAPI, pytest; React + TypeScript (pnpm).

**Spec:** `docs/superpowers/specs/2026-10-23-reviewer-eval-ratchet-design.md` (Labels: signals; Scorecard; Data: retention).

## Decisions this plan makes against the spec

- **Signals look back 60 days, not 30.** Outcomes land late: a maintainer merges or closes weeks after our review.
- **Retention clears unlabeled inputs after 90 days, rather than "30 days unless a signal points at it".** A signal can only point at a row inside its 60-day window, so 90 days leaves at least 30 days to label a signalled row without retention having to compute signals.
- **Known-clean stays a suggestion** (signal B) until revert detection exists, as the spec says. Its text asks the operator to check that the merge was not reverted.
- **The intake "operator proceeded" signal is left out.** Its only evidence is a `trigger: "operator"` that also covers public and reporter sources, so it would mostly be noise.
- **Red-team rows stay out.** There are no attack cases yet.

## Global Constraints

- No schema bump, table, section or registry key. No worker sweep and no cache. Signals and the scorecard are computed per request.
- `gather` makes at most 5 queries, whatever the number of rows.
- Store access goes through `pipeline/store.py` and `issue_triage/issue_store.py`.
- The usual conventions apply: `from __future__ import annotations`, qualified imports, precise types, present-tense comments, pnpm only.
- Gates: `uv run pytest`, `uv run ruff check .` and pyright as in AGENTS.md; `pnpm run build`, plus `pnpm exec eslint` on the changed files.

## Review Focus

1. **A labeled row is never suggested.** A suggestion that matches the verdict given is never emitted either.
2. **Our own merge is not "merged upstream by someone else"** (signal B). Check landed merge activity on that PR.
3. **A follow-up, hunter or public send-back is not an operator's** (signal G, issue side).
4. **Empty window:** a fresh store returns an empty queue and "too few cases" everywhere, never a division by zero.
5. **Retention never clears a labeled row's inputs**, and never touches rows younger than 90 days.

---

### Task 1: Store reads for labeled rows, and retention

**Files:**
- Modify: `pipeline/store.py`, `pipeline/verdict_log.py`
- Test: `pipeline/tests/test_review_verdicts_store.py`, `pipeline/tests/test_verdict_log.py`

**Interfaces:**
- Produces:
  - `Store.review_verdicts(..., labeled: bool | None = None)`: `True` keeps rows whose `label` is not null, `False` the others.
  - `Store.clear_review_verdict_inputs(before: str) -> int`: sets `inputs_gz` to NULL where `at < before`, `label IS NULL` and `inputs_gz IS NOT NULL`, and returns the count. Calls `assert_writable` first.
  - `verdict_log.RETENTION_DAYS = 90`. After a successful insert, `record` calls `clear_review_verdict_inputs(now - 90 days)` inside the same try. A failure there is reported like an insert failure; the row id is still returned.

- [ ] **Step 1:** Write failing tests:
  - `test_labeled_filter`
  - `test_clear_inputs_spares_labeled_and_recent`: an old unlabeled row is cleared; an old labeled row and a recent unlabeled row keep their blobs; the call returns 1.
  - `test_record_clears_expired_inputs`
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `Read labeled verdicts and clear expired inputs`.

### Task 2: Signals

**Files:**
- Create: `prospector_app/backend/reviewer_ratchet.py`
- Modify: `prospector_app/backend/activity.py` (make `_events_for_prs` public as `events_for_prs` and update its callers)
- Test: `prospector_app/backend/tests/test_reviewer_ratchet.py`

**Interfaces:**
- Produces:
  - `SIGNAL_WINDOW_DAYS = 60`
  - `@dataclass(frozen=True) class Facts`, holding:
    - `rows: list[VerdictRecord]`, every row in the window
    - `prs: dict[int, dict]`: `{state, head_sha}`
    - `pr_events: dict[int, list[dict]]`, activity per PR, newest first
    - `issues: dict[int, dict]`: `{state, state_reason, fix_followup, fix_run_proposal, second_opinion, fix_choice, fix_scan, links_github, fix_thread}`
    - `fix_runs: list[dict]`, the PR `fix:single` ledger rows in the window
  - `gather(since: str) -> Facts` makes these 5 reads:
    - `Store.review_verdicts(since=)`
    - `Store.pr_rows([], numbers=pr subjects)`
    - `activity.events_for_prs(pr subjects)`
    - `IssueStore.issue_rows([("fix_followup",), ("fix_run","proposal"), ("fix_run","second_opinion"), ("fix_choice",), ("fix_scan",), ("links","github"), ("fix_thread","entries")], numbers=issue subjects)`
    - `Store.runs(since=, phase="fix:single")`
  - `@dataclass(frozen=True) class Signal`: `verdict_id: int`, `reviewer: str`, `subject_kind: str`, `subject: int`, `verdict: str`, `suggests: str`, `why: str`, `key: str`
  - `signals(facts: Facts) -> list[Signal]`: newest verdict first. At most one signal per row (the first key that fires, in the order below). Labeled rows are skipped, and so is any signal whose `suggests == verdict`.
- Signals, for unlabeled rows only:

| key | reviewer, verdict | fires when | suggests |
|---|---|---|---|
| `merged-over-yellow` | security YELLOW | a landed `merge` event on the PR after the row, carrying `security_override` | GREEN |
| `merged-upstream` | security RED/YELLOW, design fail | the PR's `state == "merged"` and no landed `merge` activity event on it | GREEN / pass |
| `guidance-after-pass` | design pass | a `fix:single` row for the PR whose `stats.head_sha` is the row's head, with `stats.guidance` and `trigger` None | fail |
| `closed-unmerged` | fix-review safe | `fix_followup.closed_as == "closed"`, or any `ci_attribution.files` value `caused` | unsafe |
| `rival-better` | fix-review safe | `second_opinion` has a `gap`, `gap-open` or `gap-closed` entry, or `fix_choice` is set | unsafe |
| `sent-back` | fix-review safe | a `fix_thread` entry with `action` in (`send-back`, `solve`), `at` after the row, and `by` not in (`followup`, `hunter`, `public`) | unsafe |
| `proposed-over-hold` | trust-boundary hold | `fix_run.proposal.pr` is set | pass |
| `fixed-elsewhere` | fixability skip | `fix_scan.status == "fixed"`, or a merged PR in `links.github` | go |

- `why` is one plain sentence that names the outcome, e.g. "PR merged upstream without Prospector; check it was not reverted". The guidance or thread text, when there is one, is appended in quotes, capped at 300 characters.

- [ ] **Step 1:** Write failing tests over hand-built `Facts`:
  - one test per key that fires, plus one that does not fire for each key's near miss:
    - our own merge (`merged-upstream`)
    - a `followup` send-back (`sent-back`)
    - a fix:single row at a different head (`guidance-after-pass`)
  - `test_labeled_rows_are_never_suggested`
  - `test_empty_facts_give_no_signals`
- [ ] **Step 2:** Write `test_gather_makes_five_queries`: seed a store with 3 PR rows and 3 issue rows plus their records, then count statements with a SQLAlchemy `before_cursor_execute` listener → exactly 5.
- [ ] **Step 3:** Run → FAIL.
- [ ] **Step 4:** Implement.
- [ ] **Step 5:** Run → PASS. Also run the activity tests (`test_activity*.py`) after the rename.
- [ ] **Step 6:** Commit `Suggest labels from outcomes the store already records`.

### Task 3: Scorecard and route

**Files:**
- Modify: `prospector_app/backend/reviewer_ratchet.py`, `prospector_app/backend/app.py`
- Test: `prospector_app/backend/tests/test_reviewer_ratchet.py`, `prospector_app/backend/tests/test_verdict_label_routes.py`

**Interfaces:**
- Produces:
  - `MIN_CASES = 10`
  - `BLOCKING: dict[str, str]`: the least severe blocking verdict per reviewer. design `fail`, security `RED`, fix-review `unsafe`, trust-boundary `hold`, intake `suspicious`, fixability `skip`.
  - `@dataclass(frozen=True) class Card`, with fields:
    - `reviewer: str`, `prompt_sha: str`, `model: str`
    - `labeled: int`, `runs: int`, `failed: int`
    - `must_catch: int`, `caught: int`, `should_pass: int`, `blocked: int`
    - `under: int`, `over: int`
  - `scorecard(window_rows: list[VerdictRecord], labeled_rows: list[VerdictRecord]) -> list[Card]`:
    - Groups by `(reviewer, prompt_sha, model)`.
    - `runs` and `failed` count the window's rows. A failed row carries its reviewer's failure value.
    - The rest count labeled rows, through `verdict_labels.call`. A label counts as must-catch when its `expected` is at or above `BLOCKING[reviewer]`, and as should-pass otherwise.
    - The rates are left to the reader, so the dataclass holds counts only.
    - Sorted by reviewer, then most labeled first.
  - `GET /api/policy/reviewers` → `{"scorecard": [Card as dict], "queue": [Signal as dict], "min_cases": MIN_CASES, "window_days": SIGNAL_WINDOW_DAYS}`. It calls `gather` once, plus `review_verdicts(labeled=True)`, which also covers labels older than the window.

- [ ] **Step 1:** Write failing tests:
  - `test_scorecard_counts_by_configuration`: two prompt_shas for design; a must-catch caught, a must-catch missed, a should-pass blocked.
  - `test_scorecard_counts_failures_from_the_window`
  - `test_policy_reviewers_route` (shape, empty store)
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `Score each reviewer configuration against its labels`.

### Task 4: Policy page section

**Files:**
- Modify: `prospector_app/frontend/src/api.ts` (types `ReviewerCard`, `ReviewerSignal`; `api.reviewerPolicy()`)
- Modify: `prospector_app/frontend/src/views/Policy.tsx` (a "Reviewers" section below the trust ladder)

**Interfaces:**
- Consumes: Task 3 route.
- Produces two parts.
- **A scorecard table**, one row per configuration:
  - Columns: reviewer, prompt (first 7 characters of the sha), model, runs, failed %, labeled, catch rate (`caught / must_catch`), false-block rate (`blocked / should_pass`), under, over.
  - A rate whose denominator is below `min_cases` reads "too few cases" in muted text.
- **A queue, "Runs to label"**: each signal shows the reviewer and verdict, a link to the PR or issue page (the phase 2 panel is there), "suggests X", and the `why`.
  - The links use the routes the app already uses for PRs (`/prs/list?pr=N`) and issues (`/issues?issue=N`).
  - An empty queue reads "Nothing to label right now."
- [ ] **Step 1:** Implement.
- [ ] **Step 2:** `pnpm run build` → 0 errors; `pnpm exec eslint src/api.ts src/views/Policy.tsx` → no new errors.
- [ ] **Step 3:** Preview against a seeded throwaway store: one `merged-upstream` and one `sent-back` signal, plus labeled rows for one scorecard line. Screenshot the section, then click through a queue link to the subject's panel.
- [ ] **Step 4:** Commit `Show reviewer scorecards and runs to label on the Policy page`.

### Task 5: Gates, measurement, review

- [ ] **Step 1:** Full gates as in Global Constraints.
- [ ] **Step 2:** Time `gather` plus `scorecard` against the configured store, read-only, once this branch's schema matches the store's stamp: `uv run python -c` timing the route's functions, run from this worktree with the store URL from the environment. Put the number, the row counts and the query count in the PR. If the store's stamp is ahead of this branch, merge main first.
- [ ] **Step 3:** One fresh reviewer on the whole branch, covering the code-reviewer checks and the architecture-review warning signs.
