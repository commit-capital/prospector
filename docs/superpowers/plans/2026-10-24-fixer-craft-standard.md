# Fixer craft standard (fixer ratchet step 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every agent that writes a fix writes to one general craft standard, and every judge that asks "would a maintainer merge this" asks by the same standard.

**Architecture:** The owner already exists. `pipeline/authoring.py` holds `HOUSE_STYLE`, the rules each fix author gets: the issue-fix solo lane (and so the cross lane's candidates), the staged lane's `fix_issue`, and the PR autofix agent (`author_fix`). It also holds `DESIGN_QUESTION`, what each design judge asks: the fix review's design lens, `review_issue_fix.preferred_fix`, and the PR fix reviewer (`review_fix`). This step rewrites both in place to carry the spec's eight craft points. It does not add the spec's `issue_triage/craft.py`, because that would put a second owner beside the first. The fix-review pin moves, so it is re-pinned provisionally.

**Tech Stack:** Python 3, pytest.

**Spec:** `docs/superpowers/specs/2026-10-24-fixer-ratchet-design.md` (The craft standard; Anti-goals; Order of work, step 1).

## Global Constraints

- **The standard is general.** It names no project, file, language-specific API or case.
- **Length.** `HOUSE_STYLE` stays under 300 words and `DESIGN_QUESTION` under 200. Both are read on every fix and every design review.
- **The eight points from the spec**, carried by `HOUSE_STYLE`, and asked by `DESIGN_QUESTION` as what a maintainer would hold the merge for:
  1. write it the codebase's way;
  2. use the language's own values, types and structures, not strings of another language as the logic;
  3. reuse before writing;
  4. the smallest *complete* fix for every case the report implies;
  5. keep capabilities, narrowing rather than removing, and match a sibling that already does it;
  6. plain names and logic;
  7. test through the entry point a user reaches, asserting outcomes, never log or note text;
  8. touch only what the fix needs.
- **Existing rules stay.** The current bullets (search before adding, decide from a recorded fact, the owning layer, match the neighbours, tests like the neighbours, no refactor) are kept, merged with the new points rather than duplicated.
- **Conventions** as in AGENTS.md: present tense, no history in the prose.

## Review Focus

1. **The texts must stay general.** A future lesson must not add a project's name or a file path. Tested in Task 1.
2. **The texts must stay short.** A future lesson must not push either past its cap. Tested in Task 1.
3. **No fixer prompt may lose the standard.** The existing reach tests (`test_the_contributor_docs_and_house_style_reach_the_prompt`, `..._reach_the_agent_s_prompt`, `test_author_fix`) must still pass.
4. **The design lens and `PREFER` must still get the question verbatim** (`test_review_fix.py:159-160`).
5. **The fix-review pin test must pass after the re-pin, and no other pin may move.** Tested in Task 2.

---

### Task 1: Rewrite the standard and the question

**Files:**
- Modify: `pipeline/authoring.py` (`HOUSE_STYLE`, `DESIGN_QUESTION`; the module docstring if its description of them changes)
- Test: `pipeline/tests/test_authoring.py` (create it if it doesn't exist; otherwise add to it)

**Interfaces:**
- Produces: the same two names and types (`str`), so every caller is unchanged.

- [ ] **Step 1: Write the failing tests.**
  - `test_the_house_style_carries_the_craft_points`: each of these lower-cased phrases appears in `HOUSE_STYLE`: `"types"`, `"another language"`, `"every case the report"`, `"capabilit"`, `"entry point"`, `"never log"`. These are the points it lacks today.
  - `test_the_design_question_asks_by_them`: `DESIGN_QUESTION` mentions `"another language"`, `"every case the report"` and `"capabilit"`.
  - `test_both_stay_short`: `len(HOUSE_STYLE.split()) < 300` and `len(DESIGN_QUESTION.split()) < 200`.
  - `test_both_stay_general`: neither contains `settings.repo()`'s name, a `/`-path matching `r"\w+/\w+\.\w+"`, or `"#"` followed by a digit.
- [ ] **Step 2:** `uv run pytest pipeline/tests/test_authoring.py -q` → the two content tests FAIL; the length and generality tests may already pass.
- [ ] **Step 3:** Rewrite both texts to the Global Constraints: the eight points, the existing rules merged in, the caps. `HOUSE_STYLE` keeps its shape: a lead line, then one bullet per rule, then the no-refactor line. `DESIGN_QUESTION` stays one paragraph naming what a maintainer would hold the merge for, ending as it does now ("Judge what a maintainer would hold the merge for, not taste. Name each problem with its file:line…").
- [ ] **Step 4:** Run `uv run pytest pipeline/tests/test_authoring.py pipeline/tests/test_author_fix.py pipeline/tests/test_review_fix.py issue_triage/tests/test_fix_issue.py issue_triage/tests/test_solo_lane.py issue_triage/tests/test_review_issue_fix.py -q` → PASS.
- [ ] **Step 5:** Commit `Hold every fix author and design judge to one craft standard`.

### Task 2: Re-pin fix review, provisionally

**Files:**
- Modify: `pipeline/evals/data/reviewer_pins.json`

- [ ] **Step 1:** `uv run pytest pipeline/tests/test_reviewer_pins.py -q` → FAIL for `fix-review` only. The message names the command. Any other reviewer failing means Task 1 touched more than intended: stop and find out why.
- [ ] **Step 2:** Run the gate the way the README says, against a throwaway store:

```bash
TRIAGE_SKIP_DOTENV=1 TRIAGE_REPO=test-owner/test-repo TRIAGE_BOT_LOGIN=test-bot \
TRIAGE_STORE_URL=sqlite:///<scratch>/store.db \
uv run python -m pipeline.evals.reviewer_eval --reviewer fix-review --live
```

  A throwaway store has no labeled cases, so no agent runs. Expected output: `fix-review: PASS (provisional)` and `re-pinned fix-review`. This is honest: the live store has no fix-review labels either (0 at last read), so its gate would be provisional too.
- [ ] **Step 3:** `uv run pytest pipeline/tests/test_reviewer_pins.py -q` → PASS. `git diff pipeline/evals/data/reviewer_pins.json` shows only the `fix-review` entry changed (`prompt_sha`, `run`, `score`).
- [ ] **Step 4:** Note in the spec's "Order of work", step 1, that the standard lives in `authoring.HOUSE_STYLE` and `DESIGN_QUESTION`, and that step 4's fixer pin hashes those two. Commit both: `Re-pin fix review at the craft standard's design question`.

### Task 3: Gates and review

- [ ] **Step 1:** Run `uv run pytest`, `uv run ruff check .`, and pyright as in AGENTS.md → all clean.
- [ ] **Step 2:** Get one fresh review of the two texts as prose, as a maintainer reading them on every fix. Are they clear, general and non-redundant, and does each point say one thing once?
