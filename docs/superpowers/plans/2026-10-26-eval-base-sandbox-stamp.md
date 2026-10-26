# Eval bases qualified under today's sandbox profile — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `eval_set run` replays a base only when it was qualified under the sandbox commands the profile names today, and `eval_set build` requalifies the rest, dropping bases whose own tree can't pass those commands.

**Architecture:**
- **The problem.** Every eval-set base is a historical commit, but the sandbox runs today's `profile.verify` commands (`test_setup`, `compile_cmd`, …) against it. After the profile changed on Oct 15, four April bases (24 of 74 fixes) fail before any fix is judged:
  - `preflight:workspace-links` is missing, so R6 ends `r6-sandbox`;
  - the SDK's `ensure-build-deps` is missing, so the lane ends `base-compile` after 5–10 minutes.
- **Stamp each base.** Each base row records `sandbox`, a hash of the `VerifyPolicy` it was qualified under.
- **`run` skips stale bases.** It replays only bases whose stamp equals today's and names the bases it leaves out, so a profile edit can never again turn a run into machine faults.
- **`build` requalifies them.** It treats a base with another stamp, or none, as unbuilt. Before re-running R6, it runs `compile_cmd` once over the pristine base. A base that fails is recorded with every instance `base-compile`, so it holds no fair bug under this profile.

**Tech Stack:** Python 3, pytest, the verify sandbox.

## Global Constraints

- **No schema bump:** `sandbox` is one more key in the existing `eval-set:base` ledger stats.
- **No new table, sweep or cache.**
- **Unstamped (legacy) bases count as stale.**
- **The live profile is unchanged:** it is correct for today's tree.
- **Conventions:** AGENTS.md. Update `issue_triage/README.md` ("History replay").

## Review Focus

1. A profile edit that touches only `lint_cmd` still changes the stamp, which errs toward requalifying.
2. `run --issues` naming a bug on a stale base says so instead of reporting "no fair bug".
3. A base whose pristine compile hits a sandbox fault, rather than a compile failure, is not written off: it is left unrecorded so the next build retries it.
4. Cases (`eval-set:case`) are untouched: they qualify on the pin, today's tree.

---

### Task 1: Stamp and skip

**Files:** Modify `pipeline/evals/eval_set.py` (`record_base`, `load_manifest`, `run`). Test `pipeline/tests/test_eval_set.py`.

**Interfaces:**
- `sandbox_stamp(prof: RepoProfile) -> str`: the first 12 hex digits of sha256 over `json.dumps(asdict(prof.verify), sort_keys=True)`.
- `record_base(base, ...)` writes `sandbox` from the base dict.
- `load_manifest(...)` returns `{"bases", "fair", "stale": [base_sha, ...]}`. `bases` and `fair` cover only rows whose `sandbox` equals `sandbox_stamp(profile.active())`.
- When anything is stale, `run` prints `left out N base(s) qualified under another sandbox profile; run eval_set build to requalify: <sha12 …>`.

- [ ] Failing tests:
  - `test_a_base_from_another_sandbox_profile_is_not_run`
  - `test_an_unstamped_base_is_stale`
  - `test_the_stamp_follows_the_verify_section`: changing `compile_cmd` changes the stamp; changing a non-verify profile field does not.
- [ ] Implement, pass, commit `Replay only bases qualified under today's sandbox profile`.

### Task 2: Requalify, compile-checked

**Files:** Modify `pipeline/evals/eval_set.py` (`build`). Test `pipeline/tests/test_eval_set.py`.

**Interfaces:**
- `build` counts a base as built only when it is in `load_manifest()["bases"]`, so stale bases are requalified.
- When `prof.verify.compile_cmd` is set, `build` first runs `verify_driver.base_command_failure(base.image, cmd, lambda: verify_driver.run_phase("compile", base.image, tier=base.tier, test_cmd=cmd, base_sha=base.sha, head_sha="pristine", pristine=True))`.
- On failure, `build` records the base with every member `{"verdict": "base-compile", "detail": why}` and skips R6.
- A `prove.SandboxFault` or `verify_driver.ProbeFailure` records nothing.
- Every recorded base carries `sandbox`.

- [ ] Failing tests:
  - `test_build_requalifies_a_stale_base`
  - `test_a_base_that_cannot_compile_holds_no_fair_bug`
  - `test_a_sandbox_fault_in_the_base_check_records_nothing`
- [ ] Implement, document in the README, pass, commit `Requalify stale eval bases, compile-checked`.

### Task 3: Gates and review

- [ ] Run the full gates (pytest, ruff, pyright).
- [ ] One fresh reviewer on the whole branch.
- [ ] For the operator, on the Mac Studio:
  1. `eval_set build`: this requalifies all 13 bases, roughly 100 R6 runs, a few hours.
  2. Then re-run the smoke.
