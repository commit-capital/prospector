# Reviewer eval gate (ratchet phase 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A change to a reviewer's prompt or default model cannot merge until a live eval replays that reviewer's labeled cases and shows the new configuration is no worse than the pinned one.

**Architecture:**
- **Phase 1 recording gaps.** Fix-review and trust-boundary now record the patch their worktree held, their lane, and the inventory veto. Intake records `blocked`.
- **`pipeline/evals/reviewer_eval.py`:**
  - rebuilds a labeled case offline: a tree at the run's base, the diff by head SHA, and the recorded inputs;
  - calls the reviewer with no subject and agent bookkeeping in a throwaway store;
  - scores the result with `verdict_labels.call`;
  - keeps every result in the runs ledger.
- **`pipeline/evals/data/reviewer_pins.json`** pins each reviewer's configuration. A model-free test fails when the code's configuration differs from its pin.
- **Re-pinning.** `python -m pipeline.evals.reviewer_eval --reviewer X --live` re-pins only when the gate passes. Until a reviewer has enough cases, the gate reports instead of refusing.

**Tech Stack:** Python 3, pytest, git, the Claude/Codex CLIs through `pipeline/headless_agent`.

**Spec:** `docs/superpowers/specs/2026-10-23-reviewer-eval-ratchet-design.md` (The gate; Provisional mode; Constraints).

## Decisions this plan makes

- **A production run counts as one sample of its own configuration.** When a labeled row's `(prompt_sha, model)` equals the pin, its recorded verdict *is* the pinned side's answer, so the pinned side rarely needs re-running. Otherwise the pinned side is replayed from a checkout of the pin's commit (spec). That checkout runs `reviewer_eval replay` in a subprocess, so the pin's code, not today's, judges the case.
- **The tree at a base SHA** comes from one eval-owned clone of `TRIAGE_REPO` under the verify scratch directory (`reviewer-eval/repo`): fetched once, then a detached `git worktree` per SHA, removed afterwards. It needs no Docker and no network after the first fetch.
  - Design and fixability saw a clone with `.git`. The worktree has one too.
  - Fix-review and trust-boundary saw a materialized tree with the lane's patch applied. The replay rebuilds that with `lane_tree.materialize` plus the recorded `worktree_patch`, uncommitted.
  - The staged lane committed its tests into the base. The replay leaves them uncommitted, a small fidelity difference noted on the case.
- **Rows that can't be replayed faithfully are marked, not dropped.** That covers rows from before this phase (no `worktree_patch`), backfilled rows, and security rows whose lenses read live upstream. Each case carries `fidelity: full | partial`, and the report counts both. The gate counts both.
- **Disagreements are re-run.** When the pinned and candidate verdicts differ on a case, each side re-runs until it has `k` samples (default 3), and the majority verdict counts.
- **The model a pin records** is the code default, ignoring the environment: the `TRIAGE_AGENT_MODEL` registry default; fix-review's default review models, which resolve to the agent model; and `fixability.MODEL`.

## Global Constraints

- **No schema bump.** Eval results are runs-ledger rows (`kind="pr"`, phase `reviewer-eval:case` and `reviewer-eval:run`), validated like any `PhaseRun`.
- **The eval never writes upstream.** It never records a verdict row (`subject=None`). Agent bookkeeping goes to a throwaway store, as `issue_redteam.provider()` does. It refuses to run against a store whose schema stamp is ahead of the code.
- **Outsiders' text stays in the store.** Cases are read from it; nothing is exported to the repo (spec).
- **CI never calls a model.** Live runs are an operator's command.
- Conventions as in AGENTS.md. New env variables, if any, are registered in `settings_registry`. None are planned.

## Review Focus

1. **A case whose base SHA the eval repo lacks** (a force-pushed or GC'd SHA): the run fetches once, then marks the case `unreplayable` and reports it. The run doesn't crash, and the case doesn't count against either side.
2. **A labeled `blocked` intake row** replays with `blocked=True`, so no agent runs, and stays right.
3. **The pin's commit lacks `reviewer_eval replay`**, for a hand-written pin: refuse with a message to re-pin provisionally. Don't guess.
4. **A dirty or interrupted eval** leaves no stray worktrees. Worktrees are removed in `finally`, and `git worktree prune` runs at start.
5. **The CI test's message names the exact command to run** and the reviewer whose pin moved.

---

### Task 1: Close the recording gaps

**Files:**
- Modify: `issue_triage/review_issue_fix.py` (`full_review`, `record`), `issue_triage/fix_lane.py` (`_staged_reviews`, `_proof_bar`), `issue_triage/cross_lane.py`, `issue_triage/solo_lane.py`, `issue_triage/trust_boundary.py` (`judge`), `issue_triage/intake_audit.py` (`judge`)
- Test: `issue_triage/tests/test_review_issue_fix.py`, `test_trust_boundary.py`, `test_intake_audit.py`, `test_cross_lane.py`, `test_fix_lane.py`

**Interfaces:**
- Produces:
  - `full_review(..., worktree_patch: str | None = None, lane: str | None = None)` and `record(..., worktree_patch=..., lane=..., inventory_veto: bool = True)` add these inputs:
    - `"title"` and `"body"`, raw
    - `"worktree_patch"`, the patch applied to the reviewed tree, or `""`
    - `"lane"`
    - `"inventory_veto"`, `"1"` or `"0"`
  - `trust_boundary.judge(..., worktree_patch: str | None = None, lane: str | None = None)` adds `"worktree_patch"` and `"lane"`.
  - Call sites pass these:
    - cross lane: `worktree_patch = test_patch + pick.fix_patch`, `lane = "cross"`
    - solo lane: `worktree_patch = patch`, `lane = "solo"`
    - staged lane: `worktree_patch = test_text + fix_patch`, `lane = "staged"`
  - `intake_audit.judge` adds `"blocked": "1"/"0"` to inputs.
- [ ] **Step 1:** Write failing tests: each recorded row's `inputs` (gunzipped) carries the new keys with the call site's values. Use the cross lane, the staged lane, `judge` and intake `blocked`.
- [ ] **Step 2:** Run the tests → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests → PASS, then the whole `issue_triage/tests` → PASS.
- [ ] **Step 5:** Commit `Record what a fix review's worktree held`.

### Task 2: Replay one case

**Files:**
- Create: `pipeline/evals/reviewer_eval.py` (case loading, trees, replay)
- Modify: `pipeline/design_review.py` (rename `_entry` to `entry`, the reviewer from rendered inputs); `issue_triage/fixability.py` (split `review` into `review` and `review_rendered(clone, *, title, body, replies_block)`, which `review` calls); `pipeline/evals/issue_redteam.py` (move `provider` to `pipeline/evals/providers.py` and import it back)
- Test: `pipeline/tests/test_reviewer_eval.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Case`: `row: VerdictRecord`, `inputs: dict[str, str]`, `fidelity: Literal["full", "partial"]`, `why_partial: str | None`.
  - `load_cases(store: Store, reviewer: str) -> list[Case]`: the reviewer's labeled `pr` and `issue` rows, with inputs gunzipped.
    - A row whose inputs were cleared is skipped and counted. Labels keep their inputs, so this shouldn't happen.
    - Partial when: there's no `worktree_patch` (fix-review, trust-boundary); `prompt_sha == "backfill"`; or the reviewer is security, whose lenses read live upstream.
  - `class EvalRepo(root: Path)`:
    - `tree(sha: str) -> contextlib.AbstractContextManager[Path]`: clones `https://github.com/{settings.repo()}.git` into `root` once and fetches when the SHA is missing. It yields a detached worktree at `sha` and removes it on exit.
    - Raises `Unreplayable` when the SHA can't be found after one fetch.
  - `replay(case: Case, repo: EvalRepo) -> str`: runs the reviewer and returns its verdict in the `verdict_log` vocabulary. Never records. Per reviewer:
    - **design:** `design_review.entry(tree, diff, ...)` with the diff from `store.load_diff(head_sha)` written to a temp file, then `reviewers._design_bar`.
    - **security:** write the diff to `diff_cache.DIFFS/<head>.diff` if absent, then `security_review.review_pr(pr, title, head, bot_evidence=...)` and take `item.verdict`.
    - **fix-review:** `lane_tree.materialize(tree, tmp)`, then apply `worktree_patch` (or the fix patch when partial), then `review_issue_fix.full_review(...)` with `inventory_veto` from inputs, then `review_issue_fix.verdict_of`.
    - **trust-boundary:** the same tree build, then `trust_boundary.judge(...)` and `verdict_of`.
    - **intake:** `intake_audit.judge(title, body, notes, blocked=inputs.blocked == "1")`, then `verdict_of`.
    - **fixability:** `fixability.review_rendered(tree, ...)` and take its verdict, `failed` on `ValueError`.
  - `providers.provider(name: Literal["claude", "codex"], model: str | None) -> ContextManager[str]` moves from `issue_redteam` unchanged.
- [ ] **Step 1:** Write failing tests with `headless_agent.run_agent` canned per reviewer (the existing test fixtures show each prompt's answer shape):
  - `test_replay_<reviewer>` for all six: a seeded labeled row, a local git repo standing in for `TRIAGE_REPO` (a `file://` URL via monkeypatched `settings.repo`-derived URL), and the expected verdict
  - `test_partial_cases_are_marked`
  - `test_unreplayable_sha`
  - `test_worktrees_are_removed_on_error`
  - `test_replay_never_records`
- [ ] **Step 2:** Run the tests → FAIL.
- [ ] **Step 3:** Implement, including the two small refactors.
- [ ] **Step 4:** Run the tests → PASS, then the six reviewers' existing suites and `test_issue_redteam.py` → PASS.
- [ ] **Step 5:** Commit `Replay a labeled reviewer case offline`.

### Task 3: Result ledger and scoring

**Files:**
- Modify: `pipeline/evals/reviewer_eval.py`; `pipeline/verdict_labels.py` (move `BLOCKING` from `reviewer_ratchet` here as the one owner; `reviewer_ratchet` imports it)
- Test: `pipeline/tests/test_reviewer_eval.py`, `prospector_app/backend/tests/test_reviewer_ratchet.py`

**Interfaces:**
- Produces:
  - `samples(store: Store, case: Case, prompt_sha: str, model: str) -> list[str]`: the verdicts recorded for this configuration. The production row's own verdict counts when its configuration matches, plus every `reviewer-eval:case` ledger row for `(verdict_id, prompt_sha, model)`.
  - `add_sample(store, case, prompt_sha, model, verdict) -> None` appends `{phase: "reviewer-eval:case", started, finished, stats: {verdict_id, reviewer, prompt_sha, model, verdict, fidelity}}`.
  - `majority(verdicts: list[str]) -> str | None`: the most common verdict. On a tie it returns the most severe of the tied verdicts, which errs toward a miss counting against the candidate.
  - `@dataclass(frozen=True) class Score`: `cases`, `must_catch`, `should_pass`, `right`, `under`, `over`, `partial`, `unreplayable`.
  - `score(results: dict[int, str], cases: list[Case]) -> Score`
- [ ] **Step 1:** Write failing tests: `test_production_row_is_a_sample_of_its_own_config`, `test_majority_breaks_ties_to_the_severe_side`, `test_score_counts` and `test_blocking_has_one_owner` (`reviewer_ratchet.BLOCKING is verdict_labels.BLOCKING`).
- [ ] **Step 2:** Run the tests → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests → PASS.
- [ ] **Step 5:** Commit `Keep eval samples in the ledger and score them`.

### Task 4: Pins and the model-free test

**Files:**
- Modify: `pipeline/evals/reviewer_eval.py`
- Create: `pipeline/evals/data/reviewer_pins.json`, `pipeline/tests/test_reviewer_pins.py`

**Interfaces:**
- Produces:
  - `MIN_CASES = 10`, the same floor as the scorecard; import it from `reviewer_ratchet`'s owner (`verdict_labels.MIN_CASES`, moved there with `BLOCKING`).
  - `configs() -> dict[str, dict[str, str]]`: for each reviewer, `{prompt_sha, model}` from the code.
    - design: `PROMPT_SHA` and `settings_registry.BY_NAME["TRIAGE_AGENT_MODEL"].default`. Security, trust-boundary and intake take the same default.
    - fix-review: its `PROMPT_SHA`, with the model as the sorted join of the default review models, which resolve to the agent default.
    - fixability: `MODEL`.
  - `PINS = Path(__file__).parent / "data" / "reviewer_pins.json"`: `{reviewer: {prompt_sha, model, commit, provisional: bool, score: Score as dict | null, run: str | null}}`.
  - `python -m pipeline.evals.reviewer_eval --init` writes provisional pins at `HEAD` for any reviewer without one. It never overwrites an existing pin.
  - `test_every_reviewer_matches_its_pin`. On a mismatch it fails with: `"{reviewer}: code is {prompt_sha}/{model}, pinned {...}. Run: uv run python -m pipeline.evals.reviewer_eval --reviewer {reviewer} --live"`.
- [ ] **Step 1:** Write the failing test (no pins file yet), plus `test_init_writes_only_missing_pins`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Generate the initial file with `--init`, so every reviewer starts provisional, with no score, at this branch's commit.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `Pin each reviewer's configuration and test the code against it`.

### Task 5: The gate and the live CLI

**Files:**
- Modify: `pipeline/evals/reviewer_eval.py`; `pipeline/evals/README.md` (a "Reviewer eval gate" section)
- Test: `pipeline/tests/test_reviewer_eval.py`

**Interfaces:**
- Produces:
  - `pinned_replay(pin: dict, case: Case) -> str`:
    - creates a detached worktree of *this* repository at `pin["commit"]`;
    - runs `uv run --project <wt> python -m pipeline.evals.reviewer_eval replay --verdict-id N` with the same environment;
    - parses the verdict printed on its last line.
    - It refuses (`PinUnreplayable`) when that commit has no `replay` subcommand.
  - `gate(store, reviewer, *, k: int = 3, provider_name: str, model: str | None) -> GateResult`, where `GateResult` holds `pinned: Score`, `candidate: Score`, `passed: bool`, `provisional: bool`, `reasons: list[str]`. Steps:
    1. Load the cases.
    2. For each case, make sure the candidate has at least one sample (`replay` under the current code) and the pinned side has at least one (`samples`, else `pinned_replay`).
    3. Where the majorities differ, top both sides up to `k` samples.
    4. Score both sides.
    5. Pass when the candidate under-calls no case the pin called right, and its over-call count is no higher than the pin's.
    6. `provisional` is true while `must_catch < MIN_CASES` or `should_pass < MIN_CASES`. A provisional gate always passes and lists in `reasons` what it would have refused.
  - CLI:
    - `--reviewer X --live [--k 3] [--provider claude|codex] [--model M]` runs the gate. It writes one `reviewer-eval:run` ledger row and prints both scores, the per-case disagreements as links, and the reasons. On pass it rewrites that reviewer's pin to the current `configs()`, the current `git rev-parse HEAD`, the score, the run timestamp and `provisional`. It exits 1 on a refusal.
    - `replay --verdict-id N` prints one case's verdict, used by `pinned_replay`.
    - `--init`, from Task 4.
- [ ] **Step 1:** Write failing tests with `replay` and `pinned_replay` monkeypatched to canned verdicts:
  - `test_gate_refuses_a_new_under_call`
  - `test_gate_refuses_more_over_calls`
  - `test_gate_passes_and_repins`
  - `test_disagreements_get_k_samples`
  - `test_provisional_passes_and_reports`
  - `test_pin_commit_without_replay_refuses`
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Document the workflow in the README: change a prompt, the CI test fails, run the gate on an operator machine, commit the re-pinned file.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `Gate reviewer prompt and model changes on a live eval`.

### Task 6: Gates, a live smoke run, review

- [ ] **Step 1:** Full gates (pytest, ruff, pyright).
- [ ] **Step 2:** **Live smoke, with the operator's OK, since it spends agent time.** Run `--reviewer intake --live` against the configured store, read-only plus ledger rows. Intake is text-only and the cheapest. Report how many cases it found, the fidelity counts, the run time and the verdicts. If intake has no labeled cases, seed nothing and report "0 cases". Run the gate on a throwaway store with one hand-labeled intake case to show a full path.
- [ ] **Step 3:** One fresh reviewer on the whole branch (code-reviewer plus architecture-review).
