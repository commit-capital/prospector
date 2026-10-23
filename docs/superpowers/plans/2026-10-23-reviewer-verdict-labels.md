# Reviewer verdict labels (ratchet phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An operator can mark any recorded reviewer verdict right or wrong from the PR and issue pages, a case study's lessons can be attached as labels, and attempts that ran before capture (issue #14867) can be backfilled so they can be labeled.

**Architecture:** One label policy module (`pipeline/verdict_labels.py`) validates a label against the row's reviewer and writes it through a new `Store` column update. Two store-only routes serve a subject's verdict rows and accept a label. One React component lists a subject's rows with a label form, placed on the PR and issue pages. A CLI backfills rows from an issue's stored `fix_run`.

**Tech Stack:** Python 3, SQLAlchemy, FastAPI, pytest; React + TypeScript (pnpm).

**Spec:** `docs/superpowers/specs/2026-10-23-reviewer-eval-ratchet-design.md` (Labels: human labeling; Case studies).

## Scope against the spec's order of work

The spec puts auto sources and retention in step 2. This plan moves both to phase 3, with the signals:
- **Known-clean** auto labels need "not reverted", which the spec says stays off until revert detection exists. Until then it is a suggestion, which is a signal.
- **Red-team** rows are 8 benign controls today, and turning them into rows needs the red-team eval to record, which phase 1 deliberately stopped.
- **Retention** must spare rows a signal points at, and signals arrive in phase 3. At the measured ~1 MB/day, deferring costs about a month's growth.

## Global Constraints

- No schema bump: `label` already exists on `review_verdicts` (v38). No new table, section, sweep or cache.
- Store access only through `pipeline/store.py`; `storekit.assert_writable` before every write.
- `from __future__ import annotations`; qualified imports; precise types; comments describe the code as it is.
- Frontend: pnpm only; `pnpm run build` 0 tsc errors; `pnpm exec eslint <changed files>` adds no errors.
- Gates: `uv run pytest`, `uv run ruff check .`, `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness`.

## Review Focus

1. **Labeling a `failed` / `INCOMPLETE` row**: refused with a clear 409. A run that never judged is not a case.
2. **An expected verdict outside the row's reviewer vocabulary** (e.g. `GREEN` on a fixability row): refused, 409.
3. **Relabeling**: the last label wins and keeps `by`/`at` of the new one. Only one label per row is stored, and that is intended.
4. **A missing verdict id**: 404, never 500.
5. **Backfilling an issue twice**: the second run adds nothing for a `fix_run` already backfilled (keyed by the run's `started`).

---

### Task 1: Store column update and single-row read

**Files:**
- Modify: `pipeline/store.py` (review-verdicts block)
- Test: `pipeline/tests/test_review_verdicts_store.py`

**Interfaces:**
- Produces:
  - `Store.review_verdict(verdict_id: int) -> storekit.VerdictRecord | None`
  - `Store.set_review_verdict_label(verdict_id: int, label: dict) -> bool`: True when a row matched. Raises `ValidationError("review_verdicts.label: required dict")` for a non-dict. Calls `assert_writable` first.

- [ ] **Step 1:** Failing tests: `test_set_label_round_trip` (label reads back on both `review_verdict(id)` and `review_verdicts()`); `test_set_label_on_missing_id_is_false`; `test_set_label_validates` (a list raises `ValidationError`); `test_single_row_read_missing_is_none`.
- [ ] **Step 2:** Run `uv run pytest pipeline/tests/test_review_verdicts_store.py -q` → FAIL.
- [ ] **Step 3:** Implement with `sqlalchemy.update(...).where(id == verdict_id).values(label=label)`; return `result.rowcount == 1`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `Let a review verdict row take a label`.

### Task 2: `pipeline/verdict_labels.py`, the label policy

**Files:**
- Create: `pipeline/verdict_labels.py`
- Test: `pipeline/tests/test_verdict_labels.py`

**Interfaces:**
- Consumes: Task 1; `verdict_log.VERDICTS`.
- Produces:
  - `MARKS: tuple[str, ...] = ("real", "wrong", "intentional", "didnt-matter")`
  - `SOURCES: tuple[str, ...] = ("operator", "case-study")`
  - `class LabelRefused(ValueError)`
  - `label(store: Store, verdict_id: int, *, expected: str, by: str, note: str = "", findings: dict[str, str] | None = None, source: str = "operator") -> dict`: returns the stored label `{"expected", "findings", "note", "by", "at", "source"}`. Raises `KeyError` for a missing row. Raises `LabelRefused` when:
    - the row's verdict is its reviewer's failure value (the last entry in `VERDICTS[reviewer]`)
    - `expected` is not a judging verdict for that reviewer (the failure value is excluded)
    - a finding mark is not in `MARKS`
    - a finding key is not a decimal index into the row's findings list (`data["findings"]`, or `data["confirmed"]` for security)
    - `source` is not in `SOURCES`
    - `by` is blank
  - `note` is stripped and capped at 4000 characters.
  - `call(label: dict, verdict: str, reviewer: str) -> Literal["right", "under", "over"]`: severity order from `VERDICTS`. Phase 3's scorecard reads this; it lives here as the one owner of "what a label says".

- [ ] **Step 1:** Failing tests:
  - `test_label_stores_and_returns`
  - `test_failed_row_refused`
  - `test_expected_outside_vocabulary_refused`
  - `test_failure_value_is_not_an_expected_verdict`
  - `test_bad_mark_or_index_refused`
  - `test_relabel_replaces`
  - `test_missing_row_is_key_error`
  - `test_call_orders_by_severity`: security labeled `RED` and judged `YELLOW` is `under`; fixability labeled `go` and judged `skip` is `over`; equal is `right`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `Add the reviewer verdict label policy`.

### Task 3: Routes

**Files:**
- Modify: `prospector_app/backend/app.py`
- Test: `prospector_app/backend/tests/test_verdict_label_routes.py` (pattern: `test_clear_secret_leak_api.py`, with `data._store` and `activity.operator` monkeypatched)

**Interfaces:**
- Produces:
  - `GET /api/verdicts?subject_kind=pr|issue&subject=N` → `{"verdicts": [VerdictRecord as dict, newest first]}`. 422 for a bad kind.
  - `POST /api/verdicts/{id}/label` with body `{"expected": str, "note": str?, "findings": {str: str}?, "source": str?}` → `{"ok": true, "label": {...}}`. 404 on `KeyError`, 409 with the reason on `LabelRefused`. `by = activity.operator()["name"]`.
  - Both use the data module's store (`data.store()`), the same one `review_verdicts` is written to.

- [ ] **Step 1:** Failing tests:
  - `test_lists_a_subjects_verdicts`
  - `test_label_route_stores_operator`
  - `test_label_route_refuses_failed_row_409`
  - `test_label_route_missing_404`
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement as thin routes over `verdict_labels.label` and `Store.review_verdicts`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `Serve and label a subject's reviewer verdicts`.

### Task 4: `ReviewerVerdicts` component on the PR and issue pages

**Files:**
- Create: `prospector_app/frontend/src/components/ReviewerVerdicts.tsx`
- Modify: `prospector_app/frontend/src/api.ts` (types `ReviewVerdict`, `VerdictLabel`; `api.verdicts(kind, n)`; `api.labelVerdict(id, body)` via `postJson`)
- Modify: `prospector_app/frontend/src/views/PRDetail.tsx` and `prospector_app/frontend/src/views/IssueDetail.tsx` (one collapsed `Collapsible` titled "Reviewer verdicts" near the checks or fix panel)

**Interfaces:**
- Consumes: Task 3 routes.
- Produces: `ReviewerVerdicts({ kind, subject }: { kind: "pr" | "issue"; subject: number })`.
  - Each row shows reviewer, verdict, model, `at`, and its label if any ("right" / "should be X", plus the note).
  - A row whose verdict is not the failure value shows a small form: a select of the reviewer's judging verdicts (the current verdict is preselected as "right"), a note textarea, and a Save button.
  - Busy flag → `api.labelVerdict` → `pushToast` → reload, following `IssueFixPanel.tsx`'s `cancel`.
  - The verdict vocabularies are mirrored in one TS constant with a comment naming `pipeline/verdict_log.py`.
  - Per-finding marks are API-only in this phase.

- [ ] **Step 1:** Implement the API client methods and types.
- [ ] **Step 2:** Implement the component and place it on both pages.
- [ ] **Step 3:** `cd prospector_app/frontend && pnpm run build` → 0 errors; `pnpm exec eslint src/components/ReviewerVerdicts.tsx src/api.ts src/views/PRDetail.tsx src/views/IssueDetail.tsx` → no new errors.
- [ ] **Step 4:** Start the dev server from `.claude/launch.json`. Open a PR and an issue that have rows (seed a test store with one design row and one fix-review row if the dev store has none). Confirm the list renders and saving a label shows it; screenshot.
- [ ] **Step 5:** Commit `Show and label reviewer verdicts on PR and issue pages`.

### Task 5: Backfill pre-capture attempts

**Files:**
- Modify: `pipeline/verdict_labels.py` (add `backfill_issue` + CLI `python -m pipeline.verdict_labels backfill --issue N`)
- Test: `pipeline/tests/test_verdict_labels.py`

**Interfaces:**
- Produces: `backfill_issue(store: Store, issue: Issue) -> list[int]`: from `issue.fix_run`, append the `fix-review` row (verdict by `review_issue_fix.record`'s rule over `fix_run["reviews"]`), `trust-boundary` (from `fix_run["boundary"]`), and `intake` (from `fix_run["intake"]`, when present).
  - Each row has `subject=("issue", n)` and `base_sha` from the run.
  - `prompt_sha` is `"backfill"`. The exact prompt is unknown, so these rows never match a pin.
  - `data` carries `{..., "backfill": {"fix_run_started": <started>}, "partial_inputs": true}`.
  - `inputs` hold the report (from the issue meta), `root_cause` and `patch` (from `fix_run`), with `evidence: ""`.
  - Skips any reviewer whose row with the same `fix_run_started` already exists. Returns the new ids.
  - The CLI prints the ids for labeling in the app.

- [ ] **Step 1:** Failing tests:
  - `test_backfill_builds_three_rows_from_fix_run`
  - `test_backfill_twice_adds_nothing`
  - `test_backfill_without_fix_run_is_empty`
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Reuse `review_issue_fix.record`'s verdict rule by extracting it to `review_issue_fix.verdict_of(reviews: list[dict]) -> str` rather than restating it.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `Backfill an issue's pre-capture fix attempt as verdict rows`.

### Task 6: Gates and review

- [ ] **Step 1:** `uv run pytest`, `uv run ruff check .` and pyright, all clean; the frontend build and eslint as in Task 4.
- [ ] **Step 2:** Time `GET /api/verdicts` for one subject against the configured store (read-only) and put the number in the PR.
- [ ] **Step 3:** One fresh reviewer on the whole branch (code-reviewer + architecture-review warning signs).
- [ ] **Step 4:** After merge and deploy, the operator runs `python -m pipeline.verdict_labels backfill --issue 14867` and labels the fix-review row, using the case study's lesson 3 as the note. Lessons 1, 2 and 4 are fixer lessons and wait for the fixer ratchet.
