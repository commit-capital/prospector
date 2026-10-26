# Fixer craft scoring (fixer ratchet step 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every replayed bug our fixer fixes is also scored for craft: our fix against the human fix that merged, judged blind, plus cheap mechanical flags. The eval-set scorecard reports craft beside correctness, per fixer configuration.

**Architecture:**
- **First, a leak fix.** The cross lane's judges stop reading a base tree that already holds the human fix.
- **Replay rows** then record the fixer configuration and both fix diffs.
- **A new `pipeline/evals/craft_judge.py`** owns the pairwise craft question:
  - It is built from `authoring.HOUSE_STYLE` and `DESIGN_QUESTION`, so it judges by the standard the fixer was given.
  - It runs blind in both orders.
  - It returns win, tie or loss, with reasons tagged to the standard's points.
- **The mechanical flags** live in the same module.
- **`issue_fix_replay.run_instance`** calls the judge for each fixed run, through an injected callable as it does for `judge_contract`, and the eval-set scorecard gains a craft section.

**Tech Stack:** Python 3, pytest, the headless agent.

**Spec:** `docs/superpowers/specs/2026-10-24-fixer-ratchet-design.md` (Scoring a fixer; Order of work, step 2).

## Decisions this plan makes

- **A replay leak gets fixed first.** In a replay, `cross_lane._choose` (`preferred_fix`) and `trust_boundary.judge` read `spec.base.clone`. That is the eval base, built after the bug's fix merged, so it contains the human fix. Production is unaffected (`pre_patch` is None, so the base is the pre-fix tree). In replays, the picker can see the answer, and the host scan treats the fix's hosts as already present.
- **The fixer configuration** is `fixer_config(lane) -> {"prompt_sha", "models"}`, in `pipeline/evals/craft_judge.py`. Step 4 moves it to its pin module; until then it is only recorded.
  - It hashes `verdict_log.prompt_sha` over the lane's prompts and the standard:
    - cross and solo: `reproduce_issue.PROMPT`, `solo_lane.PROMPT`, `review_issue_fix.PREFER`, `authoring.HOUSE_STYLE`, `authoring.DESIGN_QUESTION`
    - staged: `fix_issue.PROMPT` in place of `solo_lane.PROMPT`, no `PREFER`
  - `models` is `",".join(settings.issue_fix_models())` for cross and the agent model otherwise.
- **The judge's points are numbered from `HOUSE_STYLE`'s own bullets**, parsed at import, so a lesson that rewrites a bullet renumbers by itself and nothing restates the standard. Tags are those numbers.
- **Mechanical flags in this step** are `foreign-text` and `duplicated-block`.
  - `foreign-text`: a string literal on an added non-test line that reads as SQL or a shell command.
  - `duplicated-block`: four or more consecutive added non-test lines that already appear verbatim in the base version of a touched file or a sibling file of the same extension.
  - **Lint-on-changed-lines is deferred.** The active profile names no `lint_cmd`, and compile output isn't attributable to lines without a per-tool parser. It is recorded as a deferred spec item.
- **Re-judging stored diffs under a newer judge belongs to step 4's gate.** This step makes it possible: rows keep both diffs (clipped like `fix_run.patch` at 60k each) plus `merge_sha`, from which the pre-fix tree is rebuilt.

## Global Constraints

- **The judge is a locked-down headless agent.** It reads only the pre-fix tree and the two diffs, has no gh access, and writes nothing. The report and diffs are untrusted data, as in `PREFER`.
- **Blind.** The prompt never says which fix is ours or which merged. They are "Fix A" and "Fix B", run once in each order. Two orders that disagree count as a tie.
- **Cost.** At most two judge runs per fixed instance, none for unfixed ones. Use `review_issue_fix.AGENT_TIMEOUT_SECONDS`.
- **No schema bump.** Everything new goes on existing `replay:instance` and `eval-set:run` rows.
- Conventions as in AGENTS.md. The replay and eval-set docs live in `issue_triage/README.md` ("History replay"); update that section.

## Review Focus

1. **The judge reads a tree that does not contain either fix.** A test asserts the human fix's change is absent from the judge's worktree.
2. **A run that ends without a fix gets no craft judgment and no agent call,** and counts as unjudged in the scorecard, not as a loss.
3. **A judge agent that fails or answers off-schema** yields `craft: {"result": None, "error": ...}`. The instance is still recorded and scored for correctness; one bad judgment never drops a row.
4. **A human fix that is test-only or empty** (nothing non-test to compare) is not judged, and is marked `"result": None, "error": "reference has no fix"`.
5. **An off-range point number** from the judge is dropped, not stored.

---

### Task 1: Close the replay leak in the cross lane

**Files:**
- Modify: `issue_triage/cross_lane.py` (`_choose` and the trust-boundary call: build the pre-fix tree once per run)
- Test: `issue_triage/tests/test_cross_lane.py`

**Interfaces:**
- Produces: `cross_lane._pre_fix_tree(spec: LaneSpec, workdir: Path) -> Path`. It returns `spec.base.clone` when `spec.pre_patch` is None. Otherwise it returns `lane_tree.materialize(spec.base.clone, workdir / "pre-fix", pre_patch=spec.pre_patch)`, built once and reused. `_choose` passes it to `preferred_fix`, and the trust-boundary call passes it as `base_dir`.
- [ ] **Step 1:** Failing test `test_a_replay_s_judges_read_the_pre_fix_tree`. Run the cross fixture with `pre_patch` set to a patch that changes `src/x.ts`, and capture the worktree `preferred_fix` and `trust_boundary.judge` receive. Assert that the file in that tree holds the patched content, not the base's.
- [ ] **Step 2:** `uv run pytest issue_triage/tests/test_cross_lane.py -q` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the cross, solo and fix-lane suites → PASS.
- [ ] **Step 5:** Commit `Let a replay's cross-lane judges read the pre-fix tree`.

### Task 2: The craft judge and the flags

**Files:**
- Create: `pipeline/evals/craft_judge.py`
- Test: `pipeline/tests/test_craft_judge.py`

**Interfaces:**
- Produces:
  - `POINTS: tuple[str, ...]`, `HOUSE_STYLE`'s bullets in order (1-based in the prompt).
  - `PROMPT: str`, with sections Background / Behavior / Output. It fills `__REPORT__` (`reproduce_issue.report_block`), `__WORKTREE__`, `__POINTS__` (numbered), `__QUESTION__` (`authoring.DESIGN_QUESTION`), `__FIX_A__` and `__FIX_B__` (`review_issue_fix.clip`). It asks for `{"better": "A" | "B" | "tie", "reason": str, "points": [int]}`, where `points` are the standard's points the worse fix falls short on.
  - `JUDGE_SHA = verdict_log.prompt_sha(PROMPT, authoring.HOUSE_STYLE, authoring.DESIGN_QUESTION)`
  - `@dataclass(frozen=True) class Craft`: `result: Literal["win", "tie", "loss"] | None`, `reasons: list[str]`, `points: list[int]`, `judge_sha: str`, `error: str | None`
  - `judge(worktree: Path, ours: str, reference: str, *, title: str, body: str) -> Craft`. It runs twice, (ours=A, ref=B) and (ref=A, ours=B), and maps each answer to ours/ref. Both say ours → win; both say ref → loss; anything else → tie. `points` is the union of in-range numbers, sorted; `reasons` holds both answers' reasons. An empty `reference` or `ours` gives `Craft(None, …, error=...)`. Agent unavailable or declined propagates, as `preferred_fix`'s does.
  - `flags(fix_patch: str, base: Path) -> list[str]`: a subset of `["foreign-text", "duplicated-block"]`, under the rules in Decisions.
  - `fixer_config(lane: Literal["staged", "solo", "cross"]) -> dict[str, str]`, per Decisions.
- [ ] **Step 1:** Write failing tests with `headless_agent.run_agent` canned:
  - `test_points_are_house_style_s_bullets`: `len(POINTS)` equals the number of `"\n- "` bullets in `HOUSE_STYLE`.
  - `test_agreement_in_both_orders_is_a_win_or_loss`
  - `test_disagreeing_orders_are_a_tie`
  - `test_the_prompt_never_names_ours_or_the_merged_fix`: neither filled prompt contains "merged", "human", "ours" or "our".
  - `test_off_range_points_are_dropped`
  - `test_a_garbled_answer_is_an_error_not_a_crash`
  - `test_an_empty_reference_is_not_judged` (and asserts no agent call)
  - `test_flags_foreign_text`: an added line `const q = "SELECT id FROM users WHERE name = '" + n + "'"` flags; a regex literal or a plain message string does not.
  - `test_flags_duplicated_block`: four added lines copied from a sibling file flag; four new lines don't.
  - `test_fixer_config_changes_with_the_standard`: monkeypatching `authoring.HOUSE_STYLE` changes `prompt_sha`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. The `foreign-text` detector is a regex over string literals on added lines. It matches SQL as `\b(SELECT\s+.+\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b` case-insensitively, and shell as a literal holding ` && `, ` | ` or a leading `rm `, `curl `, `sh -c`. Keep it small.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `Judge a fix's craft blind against a reference fix`.

### Task 3: Record and judge in the replay

**Files:**
- Modify: `pipeline/evals/issue_fix_replay.py` (`run_instance`, `_instance_stats`, the CLI's `run`), `pipeline/evals/eval_set.py` (`run` passes the judge and the lane's config)
- Test: `pipeline/tests/test_issue_fix_replay_run.py`, `pipeline/tests/test_eval_set.py`

**Interfaces:**
- Consumes: Task 2 `judge`, `flags`, `fixer_config`, `JUDGE_SHA`.
- Produces: `run_instance(..., judge_craft: Callable[[Path, str, str, str, str], Craft] | None = None, lane: str = "staged")`. When `judge_craft` is given and the scored ending is `fixed`:
  1. materialize the pre-fix tree (`lane_tree.materialize(base.clone, workdir / "craft-tree", pre_patch=pre_patch)`);
  2. call `judge_craft(tree, our_fix, landed_fix, title, body)`, where `our_fix` is the lane patch's non-test part and `landed_fix` is `score`'s `landed_fix_hunks`;
  3. compute `flags(our_fix, tree)`;
  4. remove the tree.

  The row gains:
  - `fixer: fixer_config(lane)`, on every row
  - `merge_sha`
  - `our_fix` and `landed_fix`, each clipped at 60k with `*_truncated` flags
  - `craft: asdict(Craft)`, or None when not judged
  - `flags`

  `eval_set.run` passes `judge_craft=craft_judge.judge` and its `lane`. `issue_fix_replay`'s own CLI `run` (staged) does the same.
- [ ] **Step 1:** Write failing tests with the existing replay-run fixtures and a stub `judge_craft` recording its arguments:
  - `test_a_fixed_run_is_craft_judged_against_the_landed_fix`: the stub receives a tree without the landed change and the two fix diffs; the row carries `craft`, `flags`, `fixer`, `our_fix`, `landed_fix`, `merge_sha`.
  - `test_an_unfixed_run_is_not_judged`: the stub is never called; `craft` is None.
  - `test_a_judge_error_still_records_the_row`: the stub returns `Craft(None, [], [], "s", "boom")`, and the row is written with oracle fields intact.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the replay and eval-set suites → PASS.
- [ ] **Step 5:** Commit `Score each replayed fix's craft against the fix that merged`.

### Task 4: The fixer scorecard

**Files:**
- Modify: `pipeline/evals/eval_set.py` (`scorecard`, `_scorecard_md`); `issue_triage/README.md` ("History replay")
- Test: `pipeline/tests/test_eval_set.py`

**Interfaces:**
- Produces: `scorecard(...)` gains `"craft"`, keyed by `fixer["prompt_sha"] + "/" + fixer["models"]`. Each entry holds:
  - `judged`, `wins`, `ties`, `losses`
  - `rate`: (wins + ½ ties) ÷ judged, or None when judged == 0
  - `errors`
  - `losses_by_point`: `{point_number: count}`, counting a loss's or tie's tagged points
  - `flags`: `{flag: count}` over fixed runs

  `_scorecard_md` adds a "Craft" table per configuration, plus a "Losing on" line naming the top three points with their bullet text truncated to 60 characters.
- [ ] **Step 1:** Write failing tests:
  - `test_scorecard_counts_craft_by_configuration`: two configurations, wins, ties, losses, errors, unjudged.
  - `test_rate_is_none_when_nothing_was_judged`
  - `test_scorecard_md_names_the_points_lost_on`
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement, and update the README section: what craft scoring is, that it is blind against the merged fix, and that it costs two judge runs per fixed bug.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `Report each fixer configuration's craft on the eval scorecard`.

### Task 5: Gates, a live judge smoke, review

- [ ] **Step 1:** Full gates (pytest, ruff, pyright).
- [ ] **Step 2:** **Live judge smoke, with the operator's OK** (two agent calls). On a throwaway two-file repo, judge a fix that compares values by assembling an SQL string against one that compares typed members. Report the result, the points, and the time. This checks the judge's direction, not the lane.
- [ ] **Step 3:** One fresh reviewer on the whole branch, covering the code-reviewer checks and the architecture-review warning signs.
- [ ] **Step 4:** Note for the operator how to run the first real measurement on the Mac Studio. Running it is theirs, because it takes hours:

```bash
uv run python -m pipeline.evals.eval_set run --name craft-1 --lane cross --passes 2
```
