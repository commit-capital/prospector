# Fixer rival and case-study cases (fixer ratchet step 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The fixer eval set gains the bugs where a human's fix beat ours in production (rival cases) and the bugs an operator names (case-study cases). Each one is replayed and craft-judged against the human fix, and also scored for correctness when the human fix's tests can serve as its oracle.

**Architecture:**
- **Storage.** A case is one `eval-set:case` ledger row that carries everything needed to replay it: issue, reference PR, merge commit, landed diff, report text, base, source and oracle kind. `eval_set.run` reads these rows directly, never through the screen.
- **Harvest.**
  - `harvest_cases` finds rivals in the issue store, keeping those whose PR merged.
  - `add_case` records an operator's case study.
  - Both qualify a case on the current verify pin, the way the history replay's own CLI does: the base is the pin, and `pre_patch` takes it back to just before the human fix (`merge^1`).
  - A case with an applicable oracle is `tests`; one whose `pre_patch` applies but that has no usable oracle is `craft-only`; anything else is recorded as unusable.
- **Replay.** `run_instance` gains a craft-only path: no R6, no oracle legs, craft judged as usual.
- **Scorecard.** Correctness counts only oracle cases; craft counts every judged case.

**Tech Stack:** Python 3, pytest, git, gh (read-only), the verify sandbox.

**Spec:** `docs/superpowers/specs/2026-10-24-fixer-ratchet-design.md` (Scoring a fixer: rival, case-study).

## Decisions this plan makes against the spec

- **The spec says to use the production attempt's `base_sha`. This plan uses the tree just before the human fix landed** (`merge^1`, as a `pre_patch` over the held pin), because:
  - production bases are reclaimed within about a day (`verify_gc` keeps two generations), so rebuilding one per case means a multi-GB Docker build, with lockfile drift;
  - the reference diff is made against `merge^1`, so it applies there;
  - it is the same pre-fix-tree construction the history replay already trusts.

  The cost: the fixer sees the code as the human did, not as our attempt did. That is the fairer comparison anyway.
- **Rows store the landed diff and report text,** so a case never depends on a clone still holding the merge commit, or on issue text staying unedited. Each row is a few KB to about 200 KB, and there are few rows.
- **Cases are keyed by `case`**, `"<source>-<issue>"` such as `rival-14867` or `case-15440`, not the bare issue number. So one issue can be both an oracle bug and a rival case without colliding in `fair`, `_recorded` or the per-bug table.
- **Rival harvest rule:** an issue whose `fix_choice` names a PR, or whose `fix_run.second_opinion` has a `gap` or `gap-open` entry, where that PR has `merged: true` according to `issue_fix_replay._gh_pr`. The merge commit and the landed diff come from the pin's clone. A merge commit the clone doesn't hold is unusable (`not-in-pin`); we don't fetch it, so the pin stays untouched.

## Global Constraints

- **Reads only.** gh (`_gh_pr`), the issue store and git in the pin's clone. Writes go only to the runs ledger (`eval-set:case`) plus `verify_gc.hold(pin.sha)`. Nothing goes upstream.
- **`--dry-run`** on `harvest` prints the candidates and their qualification without writing a row or holding a base.
- **A craft-only record** carries `oracle_pass: None` and `craft_only: True`. It never counts as correct or incorrect, and never toward precision or coverage.
- **No schema bump.** Conventions as in AGENTS.md. Update `issue_triage/README.md` ("History replay").

## Review Focus

1. **A rival PR that closed unmerged, or a `fix_choice` later undone,** is not harvested.
2. **A case whose `pre_patch` no longer applies on today's pin** is recorded `unusable`, with the reason, and `run` skips it. One stale case never crashes a run.
3. **Re-harvesting** adds no duplicate rows for unchanged cases; the latest row per `case` wins.
4. **A craft-only case gets no R6 and no oracle legs,** so no sandbox runs for them, and is still craft-judged when the lane fixes it.
5. **An issue that is both a fair oracle bug and a rival case** is replayed as both, and the scorecard keeps them apart.

---

### Task 1: Case rows

**Files:**
- Modify: `pipeline/evals/eval_set.py`
- Test: `pipeline/tests/test_eval_set.py`

**Interfaces:**
- Produces:
  - `CASE_PHASE = "eval-set:case"`
  - `@dataclass(frozen=True) class Case`, with fields:
    - `case: str`, `source: Literal["rival", "case-study"]`
    - `issue: int`, `pr: int`, `merge_sha: str`, `base_sha: str`
    - `oracle: Literal["tests", "craft-only", "unusable"]`, `detail: str`
    - `report_title: str`, `report_body: str`
    - `landed_diff: str`, `test_files: list[str]`, `nontest_files: list[str]`
  - `record_case(store, case: Case) -> None` (`trigger: "cli"`, `stats` = `asdict(case)` with `repo=settings.repo()`)
  - `load_cases(store) -> list[Case]`: this repo's rows, latest per `case`, oldest first; `_LEDGER_SCAN` with `phase=CASE_PHASE`
  - `Case.instance() -> replay.Instance`
- [ ] **Step 1:** Write failing tests:
  - `test_cases_round_trip_latest_per_case_wins`
  - `test_cases_from_another_repo_are_ignored`
  - `test_case_instance_carries_its_inputs`
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `Record rival and case-study cases with their replay inputs`.

### Task 2: The craft-only replay path

**Files:**
- Modify: `pipeline/evals/issue_fix_replay.py` (`run_instance`)
- Test: `pipeline/tests/test_issue_fix_replay_run.py`

**Interfaces:**
- Produces: `run_instance(..., oracle: bool = True)`. When `oracle` is False, it:
  - skips `validate_known_fix`;
  - runs the lane on `pre_patch`;
  - skips `score`, `candidate_scores` and `judge_contract`;
  - records `ending`, the lane evidence, `fixed: ending == "fixed"`, `oracle_pass: None` and `craft_only: True`, plus the step 2 craft fields (judged when `fixed`).

  When `transform_to_p` or `prove.flatten` raises `ValueError` or `subprocess.CalledProcessError`, the record is `ending: "unusable"` with the reason, instead of crashing.
- [ ] **Step 1:** Write failing tests:
  - `test_a_craft_only_case_skips_r6_and_the_oracle` (stub `validate_known_fix` and `score` to fail if called)
  - `test_a_craft_only_fix_is_still_craft_judged`
  - `test_a_pre_patch_that_does_not_apply_is_unusable`
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the replay suites → PASS.
- [ ] **Step 5:** Commit `Replay a case with no oracle for its craft alone`.

### Task 3: Harvest and add

**Files:**
- Modify: `pipeline/evals/eval_set.py` (CLI `cases harvest [--dry-run]` and `cases add --issue N --pr M [--dry-run]`)
- Test: `pipeline/tests/test_eval_set.py`

**Interfaces:**
- Consumes: `IssueStore.issue_rows([("fix_choice",), ("fix_run", "second_opinion")])`, `issue_fix_replay._gh_pr`, `prove.pinned`, `issue_fix_replay.transform_to_p` and `validate_known_fix`, `oracle_command`, `verify_gc.hold`.
- Produces:
  - `rival_candidates(issues: IssueStore) -> list[tuple[int, int]]`: `(issue, pr)` pairs under the harvest rule, sorted.
  - `qualify_case(source: str, issue: int, pr: int, *, pin: prove.PinnedBase, report: tuple[str, str]) -> Case`:
    1. resolve the merge (not merged → `unusable`, "not merged");
    2. `git diff --binary merge^1 merge` in `pin.clone` (missing → `unusable`, "not-in-pin");
    3. split test and non-test paths;
    4. build `pre_patch = transform_to_p(pin.clone, merge_sha, pin.sha, profile)`;
    5. when the landed diff has test files and `validate_known_fix` passes → `tests`; when `prove.flatten` of `pre_patch` applies → `craft-only`; else `unusable` with the reason.
  - `harvest_cases(*, dry_run: bool) -> int`: qualify every candidate and print one line each. Unless `dry_run`, record every row (unusable ones included, so the reason is kept) and hold the pin.
  - `add_case(issue: int, pr: int, *, dry_run: bool) -> int`: the same for one operator-named pair, with `source="case-study"`.
  - Report text comes from the issue store's meta (`fix_lane.reported_text`).
- [ ] **Step 1:** Write failing tests, with gh, the pin and R6 faked:
  - `test_rival_candidates_from_choices_and_gaps` (`gap-closed`, `covered` and `skipped` excluded)
  - `test_an_unmerged_rival_is_unusable`
  - `test_a_merge_the_pin_lacks_is_not_in_pin`
  - `test_a_rival_with_qualifying_tests_is_an_oracle_case`
  - `test_a_rival_without_tests_is_craft_only`
  - `test_dry_run_records_nothing`
  - `test_add_case_is_a_case_study`
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `Harvest rival cases and add case studies to the eval set`.

### Task 4: Run and score them

**Files:**
- Modify: `pipeline/evals/eval_set.py` (`run`, `scorecard`, `_scorecard_md`, `_correct`); `pipeline/evals/issue_fix_replay.py` (`_recorded` keys by `stats.get("case") or issue`); `issue_triage/README.md`
- Test: `pipeline/tests/test_eval_set.py`

**Interfaces:**
- Produces:
  - `run(..., cases: bool = True)`. Besides the fair bugs it replays every `load_cases` case whose `oracle != "unusable"` on the held pin recorded in the case (`_held_base(case.base_sha)`), with `oracle=(case.oracle == "tests")`.
  - Each record's stats carry `case` (the case id, or `str(issue)` for an oracle bug) and `source` (`"history"`, `"rival"` or `"case-study"`).
  - Resume and the per-bug table key by `case`. `--issues` filters both kinds by issue number.
  - `--no-cases` replays the fair bugs only.
  - `scorecard` computes precision and coverage over `history` and `tests`-oracle records only (`craft_only` excluded), adds `cases: {source: count}`, and keeps craft over every record.
- [ ] **Step 1:** Write failing tests:
  - `test_run_replays_cases_beside_the_fair_bugs`
  - `test_an_issue_both_fair_and_rival_is_run_as_both`
  - `test_craft_only_records_leave_precision_alone`
  - `test_unusable_cases_are_not_run`
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement, and document the cases in the README section.
- [ ] **Step 4:** Run the eval-set and replay suites → PASS.
- [ ] **Step 5:** Commit `Replay rival and case-study cases in every eval run`.

### Task 5: Gates, a live dry harvest, review

- [ ] **Step 1:** Full gates (pytest, ruff, pyright).
- [ ] **Step 2:** Run `uv run python -m pipeline.evals.eval_set cases harvest --dry-run` against the configured store, read-only (gh reads, no writes). It needs a pinned base on this machine. If there is none, report the candidate list from `rival_candidates` alone. Put the candidates and their qualification in the PR.
- [ ] **Step 3:** One fresh reviewer on the whole branch.
- [ ] **Step 4:** For the operator: run `cases harvest` (without `--dry-run`) on the Mac Studio to record the cases. The next `eval_set run` replays them.
