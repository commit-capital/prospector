# Fixer lessons (fixer ratchet step 5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `python -m pipeline.evals.fixer_lessons` reads where the pinned-default fixer lost on craft, asks one agent for **one** general rewrite of the house style or a fixer prompt that would have prevented the most frequent loss, checks it on the host, and writes it as a local `git apply`-able diff. Nothing is committed or recorded. If the operator applies the diff, the pin test fails and the fixer gate decides whether it stays.

**Architecture:**
- **Losses.** These are `replay:instance` records of the code's fixer configuration (`fixer_eval.config()`) whose craft under today's judge is a loss or a tie. Each carries:
  - our fix and the judge's reasons, points and mechanical flags;
  - the reference fix, from `eval_set.plan_jobs`'s job for the case (its `landed_diff`);
  - the issue's report text;
  - operator words on the issue: `fix_choice.reason`, and the notes of labeled reviewer verdicts on it.

  Losses are ordered so that those sharing the most frequent point come first, and capped.
- **The agent** is a locked-down headless agent. It reads only its prompt (an empty temp dir as `read_root`, no environment, no gh). It answers JSON:
  - `target`: one of the editable texts;
  - `old`: a line of that text, verbatim;
  - `new`: its replacement;
  - `addresses`: the case ids;
  - `why`.
- **The host decides, not the agent.** The edit must:
  - **anchor:** `old` occurs exactly once in the target's source file and lies inside the target's text;
  - **rewrite:** `new` differs from `old` and does not merely extend it;
  - **stay general:** `authoring.general(new)` passes, and `new` names no path or issue number from the losses;
  - **fit:** the target stays within its cap.

  A refused edit is sent back once with the reason. A second refusal exits 1 with the reason.
- **Output.**
  - `<verify scratch>/fixer-lessons/<UTC stamp>/edit.diff` is a unified diff of the one source file with repo-relative `a/`/`b/` paths, checked with `git apply --check`.
  - `lessons.md` beside it holds the edit, the why and the losses it addresses.
  - The command prints both paths and `git apply <diff>`.

**Tech Stack:** Python 3, difflib, pytest, headless agents.

**Spec:** `docs/superpowers/specs/2026-10-24-fixer-ratchet-design.md` (The improvement loop).

## Decisions this plan makes against the spec

- **Editable texts are those that sit verbatim in their source file:**
  - `HOUSE_STYLE` (`pipeline/authoring.py`);
  - `solo_lane.PROMPT` (`issue_triage/solo_lane.py`);
  - `reproduce_issue.PROMPT` (`issue_triage/reproduce_issue.py`).

  `DESIGN_QUESTION` is a run of adjacent string literals, so an agent's verbatim `old` can't anchor in its source. When the edit targets `HOUSE_STYLE`, `lessons.md` reminds the operator to mirror the point in `DESIGN_QUESTION`.
- **Caps.**
  - `HOUSE_STYLE` stays under its existing cap of 300 words. `authoring.HOUSE_STYLE_MAX_WORDS` and `DESIGN_QUESTION_MAX_WORDS` become constants that `test_authoring` reads.
  - A prompt may grow by at most 30 words per edit.
- **One owner for "general".** `authoring.general(text) -> str | None` holds the rules `test_both_stay_general` asserts today: no repo name, no `dir/file.ext` path, no `#123`. The test and the lessons host both call it.
- **Losses come from the code's default configuration.** That is the configuration the gate runs as the candidate, whatever `.env` overrides. Ties count as losses when they carry points.
- **Nothing is written to the store.** The command is a read and a local file.

## Global Constraints

- **AGENTS.md trust model.** The agent reads outsiders' text (issue reports and contributor diffs), so it gets no tree, no environment, no gh, and no edit rights. The host checks every property of the edit, and the agent's word decides nothing.
- **Reads only.** The store, the eval set and the pin's clone are read. The only writes are the two files under verify scratch. No commit and no upstream write.
- **Conventions.** Follow AGENTS.md, and document the loop in `pipeline/evals/README.md` ("Fixer eval gate" gains a paragraph).

## Review Focus

1. **Report text and diffs are untrusted.** A report saying "set new to …" must not get past the host checks, and the prompt must fence that text as data.
2. **An `old` that occurs twice in the file**, for example a phrase shared by two prompts in the same module, is refused, never applied to the first match.
3. **An edit that only appends** (`new` = `old` + more text) is refused as an addition.
4. **No losses** (no samples yet, or every judged run a win). The command says so and exits 0 without calling the agent.
5. **A loss whose case is no longer in the plan** (its base is stale, or it was screened out) is skipped, never crashes the read.

---

### Task 1: One owner for the standard's caps and generality

**Files:** Modify `pipeline/authoring.py` and `pipeline/tests/test_authoring.py`.

**Interfaces:**
- `HOUSE_STYLE_MAX_WORDS = 300` and `DESIGN_QUESTION_MAX_WORDS = 200`.
- `general(text: str, *, names: Iterable[str] = ()) -> str | None`: why `text` is not general, or None. It checks the repo name, a path, an issue reference, and any of `names`.

- [ ] Failing tests:
  - `test_general_refuses_a_path_an_issue_and_a_named_thing`: `src/a.ts`, `#12`, and `names=("NewAgentSetup",)` are each refused, while plain prose passes.
  - Rewrite `test_both_stay_short` and `test_both_stay_general` onto the constants and `general`.
- [ ] Implement, pass, and commit `Give the standard's caps and generality one owner`.

### Task 2: Read the losses

**Files:** Create `pipeline/evals/fixer_lessons.py` and `pipeline/tests/test_fixer_lessons.py`.

**Interfaces:**
- `@dataclass(frozen=True) class Loss`, with fields:
  - `case: str`, `issue: int`, `title: str`;
  - `ours: str`, `reference: str` (both source-only, via `craft_judge.source_only`);
  - `result: str`, `reasons: list[str]`, `points: list[int]`, `flags: list[str]`, `notes: list[str]`.
- `losses(store, issues: IssueStore, *, jobs: Callable[[set[str]], dict[str, eval_set._Job]], limit: int = 6) -> list[Loss]`:
  - selects records of `fixer_eval.config()` whose `craft.judge_sha == craft_judge.JUDGE_SHA` and whose `craft.result` is `loss`, or `tie` with points;
  - keeps the newest per case;
  - orders by the frequency of each loss's most frequent point, then newest;
  - skips a case missing from `jobs`.
  - The default `jobs` wraps `eval_set.plan_jobs(store, load_manifest(store), refresh=False, issues=None, only=..., cases=True)`.
- Notes are `fix_choice.reason` from the issue store plus the `label.note` of `store.review_verdicts(subject_kind="issue", subject=n, labeled=True)` rows. Empty notes are dropped.

- [ ] Failing tests:
  - `test_losses_are_this_configuration_s_under_today_s_judge` (another configuration, an older judge and a win are each excluded)
  - `test_losses_sharing_the_commonest_point_come_first`
  - `test_a_loss_carries_the_reference_and_the_operator_s_words`
  - `test_a_case_no_longer_planned_is_skipped`
- [ ] Implement, pass, and commit `Read the fixer's craft losses with their references and notes`.

### Task 3: Propose, check, write

**Files:** Modify `pipeline/evals/fixer_lessons.py` and `pipeline/tests/test_fixer_lessons.py`. Document in `pipeline/evals/README.md`.

**Interfaces:**
- `TARGETS: dict[str, tuple[Path, Callable[[], str]]]`: `{"HOUSE_STYLE": (authoring file, lambda: authoring.HOUSE_STYLE), "solo_lane.PROMPT": ..., "reproduce_issue.PROMPT": ...}`.
- `@dataclass(frozen=True) class Edit`: `target`, `old`, `new`, `addresses: list[str]`, `why`.
- `PROMPT`: the agent's instructions, in prose.
  - It carries the current texts of every target, then each loss in a fenced, labeled block: report, our fix, reference fix, judge reasons and points, flags, operator notes.
  - It asks for one rewrite of one line, the JSON shape, and the rules: general, a rewrite not an addition, within the cap.
- `ask(prompt: str) -> dict` runs `headless_agent.json_reply` over `run_agent(prompt, allow_gh=False, cwd=tmp, read_root=[tmp], edit_root=None, env_allow=())`.
- `check(edit: Edit, losses: list[Loss]) -> str | None` returns the refusal reason: anchor, rewrite, general (with each loss's changed paths and `#<issue>` as names), and fit.
- `render(edit: Edit) -> str`: the unified diff, which must pass `git apply --check` from the repo root.
- `propose(losses, *, ask=ask) -> Edit` makes one retry with the refusal appended, then raises `Refused(reason)`.
- `main(argv) -> int` takes `--limit N`. It exits 0 and says so when there are no losses, exits 1 on a refusal, and otherwise writes the two files and prints the apply command.

- [ ] Failing tests:
  - `test_an_edit_that_only_appends_is_refused`
  - `test_an_old_line_found_twice_is_refused`
  - `test_an_edit_naming_a_loss_s_file_is_refused`
  - `test_an_edit_past_the_cap_is_refused`
  - `test_a_refused_edit_is_retried_once_with_the_reason`
  - `test_the_diff_applies_and_changes_only_the_target_line` (real `git apply --check` on a temp copy)
  - `test_no_losses_calls_no_agent`
  - `test_the_agent_reads_nothing_but_its_prompt` (asserts `run_agent`'s lockdown kwargs)
- [ ] Implement, document, pass, and commit `Propose one general fixer lesson as a local diff`.

### Task 4: Gates and review

- [ ] Run the full gates (pytest, ruff, pyright).
- [ ] One fresh reviewer on the whole branch.
- [ ] For the operator: after a `fixer_eval --live` or `eval_set run` has produced judged losses, run `uv run python -m pipeline.evals.fixer_lessons` and read the diff. If it's worth trying, `git apply`; the pin test then fails and `fixer_eval --live` decides.
