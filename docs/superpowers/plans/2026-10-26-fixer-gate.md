# Fixer gate (fixer ratchet step 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A change to the fixer's prompts, its craft standard or its default models fails CI until `python -m pipeline.evals.fixer_eval --live` shows that the new configuration is no worse than the pinned one on a fixed gate set: no worse on the hidden oracle, and no worse on craft judged against the human fix. Below 12 cases judged on both sides, the gate is provisional.

**Architecture:**
- **One pin.** `fixer` joins `pipeline/evals/data/reviewer_pins.json`:
  - `prompt_sha` is `craft_judge.fixer_config("cross")`'s, which covers the reproduction and fix prompts, `PREFER`, `HOUSE_STYLE` and `DESIGN_QUESTION`;
  - `model` is the code's default candidate models, `"opus,sonnet,opus"`;
  - `gate_set` is the gate's case ids.
- **The pin test.** A model-free test fails on any mismatch and names the command, like the reviewer pin test.
- **Samples are the replay records `eval_set.run` already writes.** These are `replay:instance` rows, carrying `case`, `fixer`, `ending`, `oracle_pass`, `craft` (with `judge_sha`) and `our_fix`. The gate reads them, keyed by case and fixer configuration, and tops a side up by calling `eval_set.run` on the cases short of two scored samples.
  - The candidate runs in-process, with `TRIAGE_ISSUE_FIX_MODELS` forced to the code's default.
  - The pinned side runs the same command from a checkout of the newest main commit that held the pin (`reviewer_eval.pinned_commit`), with `TRIAGE_ISSUE_FIX_MODELS` set to the pin's models.
- **Craft under the candidate's judge.** A sample whose `craft.judge_sha` differs from today's `craft_judge.JUDGE_SHA` is re-judged from its stored `our_fix`. The result is cached as a `fixer-eval:craft` ledger row keyed by `(case, sha256(our_fix)[:16], judge_sha)`, so judge calls are spent once and no lane re-runs.

**Tech Stack:** Python 3, pytest, git worktrees, the verify sandbox, headless agents.

**Spec:** `docs/superpowers/specs/2026-10-24-fixer-ratchet-design.md` (The gate, Scoring a fixer).

## Decisions this plan makes against the spec

- **Samples reuse `replay:instance` rows, not new `fixer-eval:case` rows.** The replay record already holds everything a sample needs. A craft-1 eval run's records then count toward the gate, and there is one writer.
- **The fallback pinned run is that checkout's own `eval_set run --only …`.** Every commit that holds a `fixer` pin has this code, because the pin is born in this step. There is no per-case replay subcommand.
- **The gate set is fixed by the pin.** It is chosen when the pin has none, or with `--reset-gate-set`, which then needs pinned samples on any new case.
  - Pick every usable rival and case-study case, newest issue first.
  - Then top up to 12 from the manifest's fair history bugs, round-robin across bases (oldest base first) and lowest issue first within a base.

## Global Constraints

- **Reads and local runs only.** No upstream write. Ledger rows only (`replay:instance` from `eval_set.run`, plus `fixer-eval:craft` and `fixer-eval:run`). No schema bump.
- **A sample:** a `replay:instance` record whose `ending` is not a fault (`replay._is_fault`). A run with no fix scores as an oracle fail and is not craft-judged.
- **Two samples per side per case.** Gate set ≤ 12 cases. The provisional floor is 12 cases craft-judged on both sides.
- **Re-pin rule 1 (oracle):**
  - no oracle case that the pin passed in both runs fails in both of the candidate's;
  - and candidate pass rate ≥ pin pass rate − 1/(oracle cases).
- **Re-pin rule 2 (craft):** candidate craft rate ≥ pin craft rate − 1/(cases judged on both sides).
  - Both sides are judged by today's judge.
  - Craft rate is the mean over cases of the per-case mean of win = 1, tie = ½, loss = 0.
- **Conventions:** AGENTS.md. Document the gate in `pipeline/evals/README.md`.

## Review Focus

1. The operator's `.env` sets `TRIAGE_ISSUE_FIX_MODELS`. The candidate still runs, and is keyed, on the code's default models, and the environment is restored afterwards.
2. A pinned checkout run that crashes or records nothing fails the gate with its stderr tail. The gate never treats it as zero samples.
3. A sample judged under an older judge is re-judged once; a second gate run reads the cached `fixer-eval:craft` row.
4. A gate-set case that has since become unusable (not in `load_cases`, or not fair) is reported and dropped from both sides, and never counted as a candidate failure.
5. The judge cannot run (`AgentUnavailable`). The gate stops and names it, rather than scoring missing craft as a loss.

---

### Task 1: The pin and its model-free test

**Files:**
- Create `pipeline/evals/fixer_eval.py` and `pipeline/tests/test_fixer_pin.py`.
- Modify `pipeline/settings.py` (`ISSUE_FIX_MODELS` constant) and `pipeline/evals/data/reviewer_pins.json`.

**Interfaces:**
- `settings.ISSUE_FIX_MODELS: tuple[str, ...] = ("opus", "sonnet", "opus")`, used by `issue_fix_models()` as its fallback.
- `fixer_eval.config() -> dict[str, str]`: `{"prompt_sha": craft_judge.fixer_config("cross")["prompt_sha"], "model": ",".join(settings.ISSUE_FIX_MODELS)}`, ignoring the environment.
- `fixer_eval.PIN = "fixer"`. `fixer_eval.init_pin(path=reviewer_eval.PINS) -> None` adds `{**config(), "provisional": True, "gate_set": None, "score": None, "run": None}` when absent.

- [ ] Failing tests:
  - `test_the_fixer_matches_its_pin`: the message names `uv run python -m pipeline.evals.fixer_eval --live`.
  - `test_the_pin_ignores_the_environment`: `TRIAGE_ISSUE_FIX_MODELS=haiku` leaves `config()` unchanged.
  - `test_init_adds_only_a_missing_fixer_pin`.
- [ ] Implement, run `fixer_eval --init` to write the provisional pin, pass, and commit `Pin the fixer's prompts and models`.

### Task 2: Samples, the gate set, and `run --only`

**Files:** Modify `pipeline/evals/fixer_eval.py` and `pipeline/evals/eval_set.py` (`run(..., only: set[str] | None = None)`, CLI `--only a,b`). Test `pipeline/tests/test_fixer_eval.py`.

**Interfaces:**
- `eval_set.run(only=...)` replays only the jobs whose case id is in `only`, history (`"2879"`) and cases (`"rival-14867-15440"`) alike.
- `fixer_eval.samples(store, prompt_sha: str, models: str) -> dict[str, list[dict]]`: scored `replay:instance` stats by case id for that configuration, over `_LEDGER_SCAN` rows.
- `fixer_eval.choose_gate_set(manifest: dict, cases: list[eval_set.Case], size: int = 12) -> list[str]`, per the decision above.
- `fixer_eval.usable_gate_set(gate_set, manifest, cases) -> tuple[list[str], list[str]]`: (still runnable, dropped with a reason).

- [ ] Failing tests:
  - `test_only_limits_a_run_to_the_named_cases`
  - `test_samples_skip_faults_and_other_configurations`
  - `test_the_gate_set_takes_every_case_then_spreads_history_across_bases`
  - `test_a_gate_set_case_no_longer_usable_is_dropped`
- [ ] Implement, pass, and commit `Read fixer samples from the replay ledger and fix a gate set`.

### Task 3: Craft under one judge

**Files:** Modify `pipeline/evals/fixer_eval.py`. Test `pipeline/tests/test_fixer_eval.py`.

**Interfaces:**
- `fixer_eval.craft_of(store, case: str, rec: dict, *, rejudge: Callable[[str, dict], dict]) -> str | None`:
  - returns `win`, `tie`, `loss`, or None when the sample has no fix;
  - reads `rec["craft"]` when its `judge_sha == craft_judge.JUDGE_SHA`;
  - otherwise reads the cached `fixer-eval:craft` row;
  - otherwise calls `rejudge(case, rec)` and records the row.
- The default `rejudge` builds the case's instance and held base (`eval_set._held_base`, `transform_to_p(inst.pre_fix)`) and calls `craft_judge.score_craft`. `AgentUnavailable` propagates.

- [ ] Failing tests:
  - `test_a_sample_under_today_s_judge_is_read_as_is`
  - `test_an_older_judgment_is_rejudged_once_and_cached`
  - `test_a_run_with_no_fix_has_no_craft`
- [ ] Implement, pass, and commit `Judge both sides' craft under the candidate's judge`.

### Task 4: The gate and the CLI

**Files:**
- Modify `pipeline/evals/fixer_eval.py` and `pipeline/evals/reviewer_eval.py` (extract `pinned_checkout(commit: str) -> Iterator[Path]` from `pinned_runner`, which then uses it).
- Modify `pipeline/evals/README.md`.
- Test `pipeline/tests/test_fixer_eval.py`.

**Interfaces:**
- `@dataclass FixerScore`: `cases`, `oracle_cases`, `oracle_rate: float | None`, `judged`, `craft_rate: float | None`, `losses_by_point: dict[str, int]`.
- `@dataclass FixerGateResult`: `pinned: FixerScore`, `candidate: FixerScore`, `passed`, `provisional`, `reasons: list[str]`, `gate_set: list[str]`, `dropped: list[str]`.
- `fixer_eval.gate(store, *, pins_path=reviewer_eval.PINS, top_up_candidate: Callable[[list[str]], None] | None = None, top_up_pinned: Callable[[list[str]], None] | None = None, rejudge=...) -> FixerGateResult`.
  - The defaults call `eval_set.run(name=f"fixer-gate-{sha}", passes=2, lane="cross", only=set(missing), concurrency=1, refresh=False, issues=None, resume=True)`, in-process for the candidate (models env forced) and through `pinned_checkout` for the pin.
  - A pinned subprocess with a nonzero exit raises with its stderr tail.
  - The gate applies rules 1 and 2 and is provisional below 12 judged cases.
- `fixer_eval.main(argv)` takes `--init`, `--live` and `--reset-gate-set`. It appends a `fixer-eval:run` row, prints a summary, and on a pass writes the pin with `gate_set` and the candidate's score.

- [ ] Failing tests, with injected top-ups and `rejudge`:
  - `test_a_candidate_that_loses_an_oracle_case_the_pin_held_is_refused`
  - `test_a_candidate_a_case_s_worth_lower_on_craft_still_passes`
  - `test_a_candidate_two_cases_lower_on_craft_is_refused`
  - `test_below_twelve_judged_cases_the_gate_is_provisional_and_passes`
  - `test_the_gate_tops_up_only_cases_short_of_two_samples`
  - `test_the_candidate_runs_on_the_code_s_default_models`
  - `test_a_failed_pinned_run_stops_the_gate`
- [ ] Implement, document, pass, and commit `Gate fixer changes on oracle and craft against the pin`.

### Task 5: Gates, review, operator run

- [ ] Run the full gates (pytest, ruff, pyright).
- [ ] One fresh reviewer on the whole branch.
- [ ] For the operator, on the Mac Studio, after `eval_set build`: run `fixer_eval --live`. That is the first measured gate run, 12 cases × 2 cross-lane runs × 2 sides at most, with stored samples cutting it. Record its wall time and agent cost in the PR, as the spec's cost constraint asks.
