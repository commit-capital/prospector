# Issue-Fix History-Replay v0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax.

**Goal:** Run the issue-fix lane over past (issue, merged-fixing-PR) pairs on a base the machine already holds, score each run against the merged PR's own tests as a hidden oracle, and write a markdown table plus one ledger row per instance — the calibration that answers "would the lane have fixed bugs whose fix is known?" A pilot of ten instances at `--concurrency 2`.

**Architecture:** The lane core (`issue_triage/fix_lane.py::run`) gains an optional `pre_patch` so a run happens on the merged PR's pre-merge parent tree `tree(P)` instead of the raw base. Because that transform and the lane's own fix touch the same files, a new `pipeline/prove.flatten` produces one combined diff from the base by applying patches in order and diffing (where `prove.compose` refuses overlaps). One eval module `pipeline/evals/issue_fix_replay.py` selects instances, runs the lane per instance, scores from host-observed sandbox exits, and writes ledger rows + a table. v0 runs only on the machine's own pinned base (its "epoch"); it builds no per-epoch image, adds no UI, no post-scoring comparison reviewer, and no autonomy wiring — those are the fuller form, out of scope here.

**Tech Stack:** Python 3.14, pytest, the Docker verify sandbox via `pipeline/prove.py`, the lane core.

**Spec:** `docs/specs/2026-09-17-issue-fix-lane-design.md`, section "Evaluation → History replay". Where this plan is narrower than the spec (single epoch = the pin, no UI, no comparison reviewer, no `lane_autonomy`), the narrower scope is deliberate: lightweight v0.

## Global Constraints

- No existing PR queue, gate, or fence changes behavior. Edits to existing files are additive only: `pipeline/prove.py` (+`flatten`), `issue_triage/fix_lane.py` and `issue_triage/lane_tree.py` (+`pre_patch`, byte-identical when `pre_patch is None`). Everything else is new files.
- The lane's trust model is unchanged: agents judge, the host decides; agents `allow_gh=False`, reads/edits scoped to their clone; a fault is never a verdict.
- `pipeline/` and `issue_triage/` never import `prospector_app`.
- Golden: with `pre_patch is None`, `fix_lane.run` and `lane_tree.materialize` behave exactly as they do today (a test pins this).
- pyright 0 (`uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness`); `uv run ruff check .` 0; `uv run pytest` green; `python -c "import prospector_app.backend.app"` boots.
- Comments/docstrings describe present code only; precise annotations; no quoted annotations; qualified imports; tests take no docstrings; the sandbox and agent CLI are mocked in tests (real git in tmp_path where a test exercises patch/clone mechanics).
- The repo is public: no exploit strings in commits/docs.

## File Structure

| File | Responsibility |
|---|---|
| `pipeline/prove.py` (modify) | `flatten(base_clone, *patches, label)` — one diff from the base for overlapping patches |
| `issue_triage/lane_tree.py` (modify) | `materialize(..., pre_patch=None)` — apply a diff to the copied tree before the one commit |
| `issue_triage/fix_lane.py` (modify) | `LaneSpec.pre_patch`; run on `tree(P)`; flatten the proof patches when `pre_patch` is set |
| `pipeline/evals/issue_fix_replay.py` (create) | instance selection, the run loop, scoring, CLI, ledger, table |
| `pipeline/evals/__init__.py` (create if absent) | package marker |
| tests alongside each | |

---

### Task 1: `prove.flatten` — one diff from the base for overlapping patches

**Files:** Modify `pipeline/prove.py`; Test `pipeline/tests/test_prove.py`.

**Interfaces:**
- Produces: `flatten(base_clone: Path, *patches: Path | str | None, label: str) -> Path` — copies `base_clone`'s tree (without `.git`) into the verify scratch, makes it a one-commit repo, applies each non-None patch in order with `git apply` (a `Path` part is read as a file, a `str` part is piped as patch text), then returns a single patch file (named `label` + content digest, under the same `issue-fix` scratch dir `compose` uses) holding `git diff` of the result against that one commit. Unlike `compose`, patches may touch the same paths (they are applied in sequence). Raises `ValueError` when a patch does not apply, naming the patch's index.

- [ ] **Step 1: failing tests** — (a) two patches that both edit the same file flatten into one diff carrying both edits (compose would raise; flatten does not); (b) a new-file patch plus an edit patch flatten together; (c) a patch that does not apply raises `ValueError`; (d) the returned path is under the verify scratch and is a valid unified diff. Use real `git` in `tmp_path` for the base clone (mirror `test_lane_tree.py`'s hermetic `_git`/base fixture).
- [ ] **Step 2–4:** run red, implement, run green; `uv run pyright pipeline`; `uv run ruff check .`.
- [ ] **Step 5: commit** — `git commit -m "Flatten a sequence of overlapping patches into one diff from the base"`

Implementation notes: reuse the hermetic-git approach in `lane_tree.py` (`GIT_CONFIG_GLOBAL=/dev/null`, fixed identity, `--no-gpg-sign`). Apply with `git apply --whitespace=nowarn`. Name/scratch exactly like `compose` (the `_LABEL_RE` check, the `issue-fix` scratch dir, `sha256(body)[:12]`). `flatten` shares the label validation with `compose` — factor a tiny helper or repeat the two-line check; do not weaken `compose`.

### Task 2: `fix_lane` runs on a pre-patched tree

**Files:** Modify `issue_triage/lane_tree.py`, `issue_triage/fix_lane.py`; Test `issue_triage/tests/test_lane_tree.py`, `issue_triage/tests/test_fix_lane.py`.

**Interfaces:**
- `lane_tree.materialize(base_clone: Path, dest: Path, files: Sequence[VerifyAuthoredFile] = (), *, pre_patch: str | None = None) -> Path` — after copying the tree and before writing `files`, when `pre_patch` is given apply it with `git apply` against the copied tree (fail-closed: raise `ValueError` if it does not apply). `pre_patch is None` leaves today's behavior byte-identical.
- `fix_lane.LaneSpec` gains `pre_patch: str | None = None`. In `run`: pass `spec.pre_patch` to both `materialize` calls (repro clone and fix clone). The red/green proof patches: when `spec.pre_patch` is set, build them with `prove.flatten(spec.base.clone, spec.pre_patch, test_patch[, fix_patch], label=label)`; when it is None, keep the current `prove.compose(...)` calls exactly. Factor the choice into one small local helper so both legs use it.

- [ ] **Step 1: failing tests** — (lane_tree) `materialize` with a `pre_patch` that edits a tracked file yields a clone whose one commit already carries that edit; a non-applying `pre_patch` raises; **golden:** `pre_patch=None` produces the identical result as before (same tree, one commit). (fix_lane) with `spec.pre_patch` set, the fake `prove.flatten` is called for red and green with `spec.pre_patch` as the first part and the clones are materialized with the pre_patch (assert via the fixture); **golden:** with `pre_patch=None`, `prove.compose` is used (not flatten) and the existing happy-path test still ends `fixed`.
- [ ] **Step 2–4:** red, implement, green; pyright 0; ruff.
- [ ] **Step 5: commit** — `git commit -m "Let the lane run on a pre-patched tree for the history replay"`

### Task 3: instance selection (`plan`)

**Files:** Create `pipeline/evals/__init__.py` (if absent), `pipeline/evals/issue_fix_replay.py`; Test `pipeline/evals/tests/test_issue_fix_replay_plan.py` (create `pipeline/evals/tests/__init__.py` if the tree needs it — match how `pipeline/tests` is laid out).

**Interfaces (screening — pure, deterministic, no agents/sandbox):**
- `Instance` dataclass: `issue: int`, `pr: int`, `merge_sha: str`, `landed_diff: str`, `test_files: list[str]`, `nontest_files: list[str]`, `report_title: str`, `report_body: str`.
- `screen(candidate, *, base_clone: Path, pin_sha: str, profile) -> tuple[Instance | None, str | None]` implementing R1–R5 from the spec, returning `(instance, None)` on pass or `(None, reason)` on discard: R1 the issue is closed-completed with exactly one merged closing PR; R2 the PR's merge commit is an ancestor of `pin_sha` (via `git merge-base --is-ancestor`), landed diff from `git show <merge_sha>`; R3 the landed diff touches ≥1 test path and ≥1 non-test path (`diffpaths`/`profile` test rules), no dependency manifest (`gates.deps_touched`), ≤400 non-test changed lines, ≤10 files; R4 the issue's `created_at`/`updated_at` predate the PR's open and the report text never contains the PR number, its URL, or its head/merge sha (discard, do not scrub); R5 neither the issue reporter nor the PR author is a bot or a lane identity.
- `dep_declarations(base_clone: Path, merge_sha: str) -> dict` — the merged `dependencies`+`devDependencies`+`peerDependencies` maps of every workspace manifest at `merge_sha` (read from git, not the working tree). `group_by_deps(instances, base_clone) -> dict[frozen-key, list[Instance]]`.
- The candidate source is the store: closed issues with a merged closing PR, read through `issue_triage.issue_links`/`IssueStore` + `Store`; the caller passes them in so tests need no store.

- [ ] **Step 1: failing tests** — one fixture instance per rule that should pass, and one that should fail each of R1–R5 with the named reason; `dep_declarations` reads the maps from a tiny two-manifest git fixture; `group_by_deps` groups equal-declaration instances. Real git in tmp_path; no store, no network.
- [ ] **Step 2–4:** red, implement, green; pyright 0; ruff.
- [ ] **Step 5: commit** — `git commit -m "Select and group history-replay instances by rule"`

### Task 4: build `tree(P)`, validate the known fix (R6), run and score one instance

**Files:** Modify `pipeline/evals/issue_fix_replay.py`; Test `pipeline/evals/tests/test_issue_fix_replay_run.py`.

**Interfaces:**
- `transform_to_p(base_clone, merge_sha, profile) -> str` — the diff that turns the epoch/base tree into `tree(P)` = `git diff <base_sha> <P>` (P = merge commit's first parent) with dependency-manifest paths removed (so installed deps stay the image's). Returns a unified diff (the `pre_patch`).
- `oracle_test_files(landed_diff) -> list[str]` and `oracle_command(test_files, profile) -> str` (via `verify_driver.derive_test_command`).
- `validate_known_fix(base, pre_patch, landed_diff, oracle_cmd, *, label) -> tuple[bool, str]` — R6: the oracle command exits 20 twice over `flatten(base, pre_patch, <PR test hunks>)` and 0 twice over `flatten(base, pre_patch, <landed diff>)`. `(True, "")` on pass; `(False, reason)` otherwise. Sandbox faults (exits 10/30/40, timeout) return a distinct `"sandbox"` reason for the caller to retry.
- `score(instance, lane_result, oracle_run) -> dict` — from host exits only: `reproduced`, `repro_valid` (lane test red on `tree(P)`, green on `tree(P) ⧺ landed fix`), `fixed`, `oracle_pass` (merged PR's tests pass over `tree(P) ⧺ the lane's non-test hunks`), `false_accept` (both reviews safe ∧ oracle failed ∧ not `oracle_coupled` — the PR's test hunks reference a symbol the lane's fix hunks add), `false_reject`, `test_tamper`, `localized` (the lane touched a file the landed fix touched), plus per-stage seconds and token cost from `lane_result` when present.
- `run_instance(inst, *, base, profile, workdir) -> dict` — R6 gate, then `fix_lane.run(LaneSpec(..., pre_patch=transform, action="fix"), workdir=...)`, then `score`. Returns the instance record.

- [ ] **Step 1: failing tests** — mock `prove.flatten`/`prove.run_command`/`fix_lane.run` and `verify_driver.derive_test_command`; assert R6 pass/fail/sandbox-fault; assert `score` computes each field from scripted lane results + oracle exits (a fixed instance, a reproduced-not-fixed instance, a false-accept instance, an R6-fail instance).
- [ ] **Step 2–4:** red, implement, green; pyright 0; ruff.
- [ ] **Step 5: commit** — `git commit -m "Validate the known fix and score one replay instance from host exits"`

### Task 5: the command, ledger, table, resume, concurrency, pilot cost

**Files:** Modify `pipeline/evals/issue_fix_replay.py`; add docs to `issue_triage/README.md`; Test `pipeline/evals/tests/test_issue_fix_replay_cli.py`.

**Interfaces / behavior:**
- CLI `python -m pipeline.evals.issue_fix_replay <plan|run> [--epochs ...] [--limit N] [--concurrency 2] [--resume]`.
  - `plan`: print each dependency group's size, date span, and whether it matches the machine's pin; no agent/sandbox runs.
  - `run`: instances from the pin's group by default; `--limit` (pilot 10); `--concurrency` (default 2) keeps that many instances in flight (their agents overlap; the sandbox phases serialize behind the existing host phase lock — reuse the lane's locking, do not add a new one); resumable by instance (skip instances already in the ledger for this run id unless `--resume` re-runs faults); after the first ten instances, print the measured average cost per instance before continuing.
- Ledger: one `replay:run` row (config, totals) and one `replay:instance` row per instance (its scores, per-stage seconds, agent runs, token cost), appended through the store's ledger API.
- Output: a markdown table (one row per instance: issue, pr, reproduced, repro_valid, fixed, oracle_pass, false_accept, seconds, cost) plus an aggregate line, written to `<verify scratch>/replay/<run-id>/table.md` and printed.
- Exit 0 when the run completes (even with failed instances — a failure is data), non-zero only on a setup error (no base, bad `--epochs`).

- [ ] **Step 1: failing tests** — mock `run_instance` and the store: a run over 3 scripted instances writes 3 `replay:instance` rows + 1 `replay:run` row that parse through `storekit.parse_run`; the markdown table has a row per instance and an aggregate line; `--limit` caps instances; `--resume` skips instances already in the ledger; `plan` prints groups without running instances.
- [ ] **Step 2–4:** red, implement, green; pyright 0; ruff; `python -m pipeline.evals.issue_fix_replay --help` exits 0.
- [ ] **Step 5: docs** — a short "History replay" subsection in `issue_triage/README.md`: what it does, the two commands, that it runs on the machine's own pinned base and writes a table + ledger rows, and that the numbers gate go-live.
- [ ] **Step 6: commit** — `git commit -m "Run the history replay from the command line, scoring a batch and writing a table"`

---

## After this plan

The Mac Studio pulls main, pins its base, and runs `python -m pipeline.evals.issue_fix_replay run --limit 10 --concurrency 2`; the pilot's table + cost per instance come back for a decision on scaling up. The fuller form (multi-epoch builds, a Control-tab card, the post-scoring comparison reviewer, `lane_autonomy` reading these numbers) follows the pilot.
