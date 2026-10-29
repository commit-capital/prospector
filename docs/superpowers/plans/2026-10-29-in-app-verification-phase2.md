# In-app verification, phase 2: the e2e baseline and the regression signal

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Pinning a base captures which of its e2e tests pass reliably
offline. Verifying an app-touching PR runs those tests over the PR's build,
and a confirmed failure demotes `verified-fix` to `regressed` (or `escalate`
when the PR edits the failing spec). The PR page shows the result.

**Spec:** `docs/superpowers/specs/2026-10-28-in-app-verification-design.md`,
Goals "A curated regression suite per pinned base" and "Regression check on a
PR", and "My calls". Phase 1 (`2026-10-28-in-app-verification-phase1.md`)
built the image, the `e2e` phase, `run_e2e`, the artifact reader, the report
parser and the coverage command.

**Out of scope:** in-app repro, screenshots in Supabase, issue-fix lane parity
(each its own later plan). Failing tests' screenshots are not kept.

## Decisions

1. **Granularity is the test, not the file.** The checkpoint measured 202 of
   208 tests reliable, but excluding a whole spec file for one bad test loses
   its good tests. The baseline is the set of tests (file + title path) that
   passed in all 3 runs. A PR run runs every file holding a baseline test and
   judges only baseline tests; a non-baseline test in the same file is ignored
   whatever it does.
2. **The baseline lives in the pin** (`verify_base` registry, per host), next
   to `baseline_failing`, because it describes the image the pin names and
   moves with it. Shape: `e2e_baseline: {spec file: [titles]}` (about 12 KB
   for 202 tests), `e2e_uncovered: [{file, title, verdict, cause, evidence}]`
   for the flaky and failing tests, and `e2e_skipped: int`. A pin without
   `e2e_baseline` (no `verify.e2e`, a tier-0 base, a base without Playwright,
   or a pin written by older code) has no e2e lane: PRs record
   `skipped_reason: "no-e2e-baseline"` and nothing gates.
3. **Pin refusal mirrors the suite baseline.** `prepare_base` refuses to pin
   when any of the 3 runs did not boot, or when more than
   `BASELINE_MAX_FAILING_SHARE` (25%) of the tests that ran (not skipped) are
   flaky or failing (`implausible_e2e_baseline`), or when no test passed
   reliably in a listed spec file. Cost: about 75 minutes added
   to each pin on a tier-1 e2e profile.
4. **The base's `tests/e2e/` judges the PR.** The host drops every hunk under
   the config's directory from the PR's patch (`diffpaths.filter_diff`) before
   the e2e phase, so the base's specs, config and fixtures run against the
   PR's app code. No container change.
5. **When it runs.** After an accepted, confirmed red→green and passing
   merge-gate lanes, after the regress leg (whatever regress found), on PRs
   that touch app code: `gates.e2e_relevant(paths, root)` is true when some
   changed path is not docs (`*.md`, `docs/`), CI (`.github/`), a test path
   (`diffpaths.is_test_path`), or under the e2e root. Both the PR's-own-test
   lane and the authored-test lane.
6. **A failure is confirmed by one re-run.** Run 1 runs the baseline files. A
   baseline test that is missing from the results, or whose status is not
   `passed`/`flaky`, fails. If any fail, run 2 re-runs only the files holding
   them. A regression is a test failing in both runs. A failing exit whose
   report shows nothing failed, or a run that does not boot, fails every
   baseline test it was asked for. A regression that rests on an exit outside
   the sentinel set (no report, a timeout, a container fault) holds the run as
   a harness fault rather than blaming the PR. An e2e patch conflict (the
   PR's app code will not apply without its suite changes) escalates.
   Results are keyed by spec file and title path, with the Playwright project
   leading the title when the config names one.
7. **The gate.** `gates.e2e_verdict(e2e)` returns `"escalate"` when every
   confirmed regression is in a spec file the PR edits (a deliberate UI change
   for a human to read), `"regressed"` when any is not, else None.
   `verify_outcome` order after the existing escalates: suite regress
   confirmed → `regressed`; e2e `regressed` → `regressed`; lanes; e2e
   `escalate` → `escalate`; `verified-fix`. So the earlier escalates keep
   primacy and a hard regression is never masked by an e2e escalate.
   `_authored_lane_outcome` applies the same two lines. `verify_run_errored`
   holds a record whose first run failed and whose confirm never concluded.
   A record with no `e2e` key does not gate.
8. **Schema bump to 44.** A PR's `verify.signals` gains `e2e` and the pin gains
   `e2e_*`. An older checkout re-running the judge over stored evidence
   (`commit_outcomes`) would ignore a confirmed e2e regression and write
   `verified-fix`.
9. **Playwright file filters are anchored.** Playwright treats each file
   argument as a case-insensitive unanchored regex over the absolute path
   (`forceRegExp`), so `a.spec.ts` also matches `xa.spec.tsx`. Each argument
   becomes `/(?:^|\/)<escaped path>$/`. The host lists the spec files from the
   pinned base clone (`e2e_specs`), so the in-container `find` listing goes.

## Data shapes (`pipeline/wire.py`)

```python
class VerifyE2eUncovered(TypedDict):
    file: str; title: str; verdict: str; cause: str | None; evidence: str | None

# VerifyPin gains
e2e_baseline: NotRequired[dict[str, list[str]] | None]
e2e_uncovered: NotRequired[list[VerifyE2eUncovered]]
e2e_skipped: NotRequired[int]

class VerifyE2eSignal(TypedDict, total=False):
    ran: bool
    skipped_reason: str          # no-e2e-baseline | not-app-touching | red-green-not-clean | lane-<x>-failed | e2e-patch-conflict
    baseline_tests: int
    exit_first: int | None
    exit_confirm: int | None
    failing_first: list[str]     # "file › title", capped at E2E_MAX_LISTED
    regressions: list[str]       # failed both runs, capped
    regression_count: int
    edited: list[str]            # spec files of regressions the PR edits
    problem: str | None          # a run's boot problem or top-level error
```

## Tasks

### Task 1: anchored filters, host-listed specs, no headless shell, top-level errors
- `verify_driver.e2e_test_cmd(cfg, specs)` requires a non-empty list and
  passes each spec as an anchored regex; `e2e_specs(clone, cfg)` lists
  `*.spec.ts` under the config dir minus excludes. `run_e2e`'s `specs` becomes
  required; `e2e_coverage_cmd` passes `e2e_specs(base_clone_dir(sha), cfg)`.
- `prefetch_browsers` adds `--no-shell` for Playwright ≥ 1.49.
- `e2e_coverage_cmd` adds each booted run's `problem` to the report's notes.
- Tests: an anchored filter escapes `.`, `+`, `(`; a spec name that is a
  prefix or suffix of another does not match it (assert the regex against
  both paths in Python); `--no-shell` present at 1.62.1, absent at 1.48.0.

### Task 2: the baseline at pin time
- `e2e_coverage.baseline(cov) -> (dict[str, list[str]], list[VerifyE2eUncovered])`.
- `verify_driver.capture_e2e_baseline(image, tier, sha) -> wire pin fields`:
  3 full runs, refuse on a run that did not boot or on
  `implausible_e2e_baseline`. Called from `prepare_base` when the profile has
  `verify.e2e`, tier is 1 and the clone pins Playwright; else the pin records
  `e2e_baseline: None`.
- `store.save_verify_base` validates the new keys' types.
  `verify_driver.pinned_e2e_baseline(store) -> dict[str, list[str]] | None`.
- Tests: 3/3 joins, 2/3 and retried-flaky are uncovered with cause; one
  unbooted run refuses; 30% failing refuses; no e2e config → `None`, no runs.

### Task 3: the PR's regression run
- `verify_driver.e2e_patch(head, patch, root) -> Path | None` drops hunks
  under `root`.
- `verify_driver.e2e_regression(...) -> wire.VerifyE2eSignal`: the two-run
  rule in Decision 6, pure over injected `run_e2e`.
- `verify_driver.verify_pr` gains `e2e_baseline`, runs the leg after regress,
  records `ev["e2e"]`; `verify_pr.py` loads the pin's baseline and stores
  `signals["e2e"]`. Commit adds an `e2e` finding listing the regressions.
- Tests: no failures → one run; a failure that passes on confirm → no
  regression and a flake note; a confirmed failure; an unbooted PR app twice →
  every requested test regresses and `problem` is set; a missing baseline test
  counts as failed; a non-baseline test's failure is ignored; edits to the e2e
  dir never reach the patch; non-app PRs and pins without a baseline skip.

### Task 4: the gate (security-focused tests)
- `gates.e2e_relevant`, `gates.e2e_verdict`, the `verify_outcome` /
  `_authored_lane_outcome` / `verify_run_errored` changes, the schema bump.
- Tests: confirmed regression demotes `verified-fix`; edited-only regression
  escalates; mixed edited and unedited regresses; suite regress plus e2e
  escalate stays `regressed`; unfaithful blind plus e2e regression stays
  `escalate`; a flake does not gate; `regression_count` 0 with a non-empty
  `regressions` list (forged shape) does not pass as clean — the verdict reads
  `regressions` and the count, either non-empty gates; an `edited` entry that
  names no regression's file does not turn a regression into escalate;
  incomplete confirm holds; no `e2e` key changes nothing.

### Task 5: the PR page
- `verify_view` explains a regressed/escalate outcome caused by e2e and maps
  it in the disposition consequence text (`gates` line 1907).
- `api.ts` `VerifySignals.e2e`; `VerifyPanel` shows an `e2e` chip beside the
  lane chips (✓ n tests / ✗ n regressions / – skipped reason) and, when there
  are regressions or a problem, a collapsible list.

### Task 6: gates and PR
`uv run pytest`, `ruff`, `pyright`, frontend build and eslint on changed
files, the release guard, architecture-review subagent over the diff.
