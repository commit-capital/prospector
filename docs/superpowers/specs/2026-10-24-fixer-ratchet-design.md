# Fixer ratchet

## Why

The issue-fix factory's fixes pass their tests and still lose to a human's.
The operator's own words for what is missing: cleaner code, more readable, more
elegant. Concretely, a fix a maintainer would merge, not one that turns a test
green. One fix compared values by assembling SQL strings inside TypeScript
where the human's used the language's own types and compared members
directly. On #14867 the fix underfit the report, tested a log line instead of
the behavior, and proposed cutting a capability to clear a review.

Tests cannot see any of this: both fixes pass the same reproductions. So the
fixer needs what the reviewers now have, aimed at craft as well as
correctness:
- a standard it is held to;
- a way to score it against fixes humans actually merged;
- a gate on changes to it;
- a loop that turns its losses into better instructions.

This must not overfit to the cases that prompted it. The standard is general
software craft, and a lesson only lands when it improves the fixer across the
whole eval set.

## Goals

- One short, general **craft standard**. It is what good code means here, and
  the fixer writes to it while the design reviewers judge by it.
- Every fixer configuration is scored on past bugs with a merged human fix for
  **correctness** (the hidden oracle, as today) and for **craft** (a blind
  comparison against the human fix).
- A change to the fixer's prompts, the craft standard or the fixer's default
  model can't merge without a live eval showing it is no worse.
- Losses become **proposed** edits to the standard or prompts. An operator
  reviews them, and the gate measures them.

## Non-goals

- Fully automatic self-modification. Proposals are diffs a person commits.
- Grading code style a formatter or linter already owns: whitespace, import
  order, quote style.
- Repository-specific rules in the standard. Those belong in the target repo's
  own contributor docs, which the fixer already reads
  (`authoring.docs_from_tree`).

## Anti-goals

- **A standard that grows with every case.** It stays under about 250 words. A
  lesson rewrites or replaces a line, and the standard never gains a line
  naming a project, a file or a case. The operator considers append-only
  instruction files a design smell, so this one must not become one.
- **A judge that rewards size, cleverness or its own taste over the
  codebase's.** It compares blind, in both orders, with ties allowed. It is
  told the target codebase's conventions win over general preference.
- **Craft bought with correctness.** The oracle bar holds first, and a
  "cleaner" fix that fixes less is a loss.
- **A cost blowup.** Fixer replays are full lane runs. The eval set is small
  and fixed per gate run, and samples are reused like the reviewer eval's.

## The craft standard

`issue_triage/craft.py` owns one constant, `STANDARD`, plus its hash. It is
injected into the fixer prompts (`solo_lane.PROMPT`, `fix_issue.PROMPT`, the
cross lane's candidates) and into the judges that already decide on design: the
fix review's design lens and `review_issue_fix.preferred_fix` (`PREFER`). One
owner means the fixer is graded by the rule it was given.

It does not go into the PR design reviewer (`pipeline/design_review.py`). That
reviewer judges contributors' PRs against their repo's conventions, which is a
different job. Because the fix review's design lens reads the standard, an edit
to it moves the `fix-review` pin as well as the fixer's, so it passes both
gates.

The starting text is the builder's to word; these are its points:

1. Write it the way this codebase already writes that kind of code: its
   language constructs, types, helpers and layering. Read the neighbors first.
2. Express logic in the language's own values, types and structures. Do not
   assemble or compare strings of another language (SQL, shell, regular
   expressions, markup) as the implementation, unless the code around it
   already does exactly that.
3. Reuse before writing: call the helper that exists; don't restate it.
4. Fix the cause the report describes, completely, where it lives. Aim for the
   smallest change that handles every case the report implies, not the
   smallest that passes the test.
5. Keep capabilities. Narrow a risky path rather than remove a feature. When a
   sibling implementation already does the thing, match it and say so.
6. Name values for what they hold. Keep important logic plain, not clever.
   Comment only the why that isn't obvious.
7. Test the behavior through the entry point a user reaches, and assert
   outcomes, not log lines or notes.
8. Touch only what the fix needs, and leave those lines easier to read than
   you found them.

The #14867 lessons are points 4, 5 and 7, and the SQL-in-TypeScript fix is point
2. None of them names its case.

## Scoring a fixer

The eval set is `pipeline/evals/eval_set.py`'s manifest: closed bugs with a
merged human fix, each with a held base and a hidden oracle. Two more kinds of
case join it, as rows with `source` set. They need not pass the replay screen
(R1–R5). Their base is the production attempt's recorded `base_sha`. When the
reference PR carries tests that apply there, those are its oracle; otherwise
the case is **craft-only**. A craft-only case counts toward the craft rate but
not toward rule 1.
- **rival:** a production issue whose rival PR merged, where an operator went
  with that PR (`fix_choice`) or its tests found cases our fix misses
  (`second_opinion` `gap`/`gap-open`). The rival's merged diff is the
  reference.
- **case-study:** an issue the operator names, with the reference PR given.

Per case and per fixer configuration (the prompts' and standard's hash, plus
the models), one lane run under `issue_fix_replay` records:

- **correctness:** oracle pass or fail, and coverage, exactly as today.
- **craft:** a blind pairwise judgment of our fix against the reference. It is
  `preferred_fix` asked "which would a careful maintainer of this repository
  rather merge", run in both orders and given the craft standard.
  - The result is `win`, `tie` or `loss`. The two orders disagreeing is a tie.
  - The reasons are tagged with the standard's point numbers, so a scorecard
    can say which points the fixer keeps losing on.
- **mechanical flags**, reported but not gated: new lint or type warnings on
  changed lines from the profile's sandbox commands, string literals in the
  diff that look like code in another language, and blocks duplicated from the
  base. Each is cheap evidence for the judge's reasons.

**What is "the fixer".** It is the lane production runs: the cross lane
(`fix_review_runner`), three candidates, the design pick and the reviews. Its
craft is judged on the fix it picks. The fixer pin's hash covers:
- the reproduction and fix prompts it runs (`reproduce_issue`, `solo_lane`);
- `PREFER`;
- `craft.STANDARD`.

Its models are `issue_fix_models()`'s defaults. The staged lane's
`fix_issue.PROMPT` also gets the standard but is not gated, because production
doesn't run it.

**Samples and missing fixes.** Each side gets two lane runs per case, a
nondeterministic lane being too noisy at one.
- A run that proposes no fix (`no-fix`, `fix-unproven`, held, …) counts as an
  oracle fail, so proposing less never helps. It is not craft-judged.
- Craft is judged per run that has a fix. A case's craft is the mean over its
  judged runs.

**Craft is re-judged under the candidate's judge.** Every ledger row keeps the
picked fix's diff. When a change touches the standard or `PREFER`, the gate
re-judges the pinned side's stored diffs with the candidate's judge before
comparing. An edit therefore can't pass by loosening the bar. That costs judge
calls only, no lane runs. Craft verdicts are keyed by case, fixer
configuration and judge hash.

Results are runs-ledger rows (`fixer-eval:case`), reused across gate runs the
way `reviewer-eval:case` samples are. A configuration's score:
- **oracle pass rate** over the cases;
- **craft rate**, (wins + ½ ties) ÷ cases judged;
- **losses by standard point.**

## The gate

`fixer` joins `pipeline/evals/data/reviewer_pins.json`. Its prompt hash covers
the fixer prompts and `craft.STANDARD`, and its model is the lanes' default
models. The model-free pin test fails on any change and names the command:
`uv run python -m pipeline.evals.fixer_eval --live`.

**The gate set.** A gate run uses a fixed **gate set** of up to 12 cases, not
the whole manifest:
- every rival and case-study case;
- topped up with oracle cases from the manifest, chosen deterministically (by
  issue number) across its dependency groups.

The pin records the gate set's case ids, so both sides run on the same cases.
The set changes only by re-pinning, which keeps a run to hours, not days: 12
cases × 2 runs of the cross lane.

The live eval runs the candidate on every gate-set case that lacks its two
samples. The pinned side comes from stored samples, falling back to a checkout
of the newest main commit that held the pin, as in the reviewer gate.

The candidate re-pins when:
1. no oracle case the pin passed in both runs fails in both of the candidate's
   runs, and its overall oracle pass rate is no lower than the pin's minus one
   case's worth, and
2. its craft rate, both sides judged by the candidate's judge, is no lower than
   the pin's minus one case's worth, which leaves room for judge noise on small
   sets.

Until 12 gate-set cases have been judged on both sides, the gate is
provisional: it passes and reports what it would have refused. Raise the floor
once a measured run's cost allows a bigger set.

## The improvement loop

`python -m pipeline.evals.fixer_lessons` reads the latest eval's losses:
- our diff against the reference;
- the judge's tagged reasons;
- the mechanical flags;
- operator labels and case-study notes on those issues.

It asks one agent for **one** edit to `craft.STANDARD`, or to a fixer prompt,
that would have prevented the most frequent loss. The edit must be:
- general: no project, file or case named;
- a rewrite or replacement, never only an addition;
- within the standard's length cap.

It writes the edit as a local diff with the losses it addresses. Nothing is
committed. The operator applies it if they agree, the pin test fails, and the
gate decides whether it stays.

## Order of work

1. The standard, in the fixer prompts and the two design judges. It lives
   where both already were, `pipeline/authoring.py`: `HOUSE_STYLE` for every fix
   author and `DESIGN_QUESTION` for every design judge. That keeps one owner, so
   no `craft.py`. Step 4's fixer pin hashes those two texts. This lands value at once and changes every prompt hash, so it
   ships with a provisional re-pin.
2. Craft scoring in `issue_fix_replay` (pairwise and mechanical flags), plus a
   fixer scorecard.
3. Rival and case-study cases in the eval set.
4. The fixer pin, the model-free test, `fixer_eval --live` and provisional mode.
5. `fixer_lessons`.

Each is its own PR.

## Constraints

- **AGENTS.md trust model.** The fixer and judges are locked-down headless
  agents, the judge reads only text and the two diffs, and the eval never
  writes upstream.
- **Store rules.** Ledger rows only, no new table.
- **Cost.** A gate run is at most the eval set size × one lane run for the
  candidate. Measure the wall time and agent cost of one run on the Mac Studio
  before setting the provisional floor.
- **The standard is prose an agent reads on every fix.** Keep it short, like
  AGENTS.md.

## Left to the builder

- The standard's exact wording, within the points and the cap.
- How `PREFER` is extended for ties and tags. A separate prompt is fine if it
  stays one owner of the craft question.
- The mechanical flags' detectors, beyond the three named.
- Which issues seed the rival and case-study sources first. The issue where
  the operator chose the outside PR over our SQL-string fix is the obvious
  first.

## Calls made without the operator

- Craft is judged against a merged human fix, not against an abstract score.
- The standard is ~250 words, general, and rewritten rather than appended.
- The gate is oracle-first, then craft rate with a one-case noise margin;
  provisional below 20 cases.
- Lessons are proposed as local diffs, one at a time, never committed by the
  agent.
- Fixer pins share the reviewer pins file and its gate pattern.
- The fixer is the production cross lane, judged on its picked fix, with one
  `fixer` pin.
- Two lane runs per side per case. A run with no fix counts as an oracle fail
  and gets no craft judgment.
- Stored diffs are re-judged under the candidate's judge when the standard or
  `PREFER` changes.
- A standard edit passes both the fixer gate and the fix-review gate. It
  stays out of the PR design reviewer.
- Rival and case-study cases skip the replay screen and are craft-only when
  their reference carries no applicable tests.
- A fixed gate set of up to 12 cases is recorded in the pin, with a provisional
  floor of 12 judged cases.
