# Issue-fix factory — design

Date: 2026-09-22
Status: proposed, for review

## Goal

A pipeline that takes a reported Paperclip bug and, without a person in the
loop, clarifies it, reproduces it, fixes it, proves the fix, and proposes it.
The one human act is the final "merge this" click. The factory earns that
autonomy by being measured: it proposes a merge only for fixes whose evidence
clears a bar whose precision has been demonstrated on the evaluation set.

This replaces tuning the current lane (`issue_triage/fix_lane.py`) one bug at a
time. The lane's sandbox, clones, proof primitives and fences stay; its
decision structure changes.

## What the backtests showed

Two days of replaying past bugs (September 2026) taught four things:

1. **Opinions do not add up to reliability.** The lane chains six agent
   judgments: reproduce, judge, fix, two refuting reviewers, then preservation
   tests. Each is a sample, not a proof. Before the preservation change, both
   reviewers approved an over-strict fix in 3 of 5 passes. After making them
   stricter, they rejected 3 of 3, including fixes that matched what the
   maintainers shipped. A stricter gate turned false accepts into false
   rejects.
2. **Many reports do not determine the answer.** Issue 2257 accepted "400 or an
   empty list"; the maintainers shipped a 422 and a `null` sentinel the report
   never mentions. No fixer, however capable, can recover intent the report
   does not carry. A person asks; the lane had no way to.
3. **One bug is not a measurement.** The same bug reached a different ending on
   almost every pass. Every change judged on one bug was judged on noise.
4. **The harness is part of the product.** Era bases, the known-fix check,
   contaminated test runs, and a shared scratch file that let parallel passes
   prove against each other's tests all had to be fixed before a result meant
   anything.

## Principles

- **Executable evidence decides; agents propose.** A fix is judged by tests the
  host runs and a behavior diff the host computes. Agents write code, tests and
  questions. They never hold a veto or a pass on their own say-so.
- **Ask when the report leaves a real choice.** When the fix's observable
  behavior depends on a decision the report and the codebase leave open, the
  factory asks the reporter or a maintainer one short question, or, where the
  codebase has a clear convention, follows it and records that it did.
- **Redundancy over strictness.** Several independent fixes, and agreement
  among them, are a stronger signal than one fix put through harsher review.
- **Every change to the factory is measured on the whole evaluation set,**
  several passes per bug, one change at a time.

## Pipeline

```
report ─▶ 1 intake ─▶ 2 spec ─▶ 3 fix ×N ─▶ 4 judge ─▶ 5 select ─▶ 6 propose ─▶ merge click
             │           │
             └── ask ◀───┘   (reporter or maintainer, one question at a time)
```

### 1. Intake

Decide whether the report can be worked at all: does it name a behavior, an
input, and what should happen instead? This is a rubric-based grade, with the
existing `repro_grade.py` as the start. A report that falls short gets one
comment asking for the missing piece, such as steps, the input, or expected
behavior, and waits. A report that describes a feature, not a defect, is routed
out, not attempted.

### 2. Spec

Turn the report into **acceptance tests** before any fix exists:

- **Reproduction tests** fail on the base for the reported reason, as today.
- **Preservation tests** pass on the base and pin the neighbouring behavior the
  fix must keep, as today (#306).
- **An ambiguity check.** The spec agent lists every observable decision the fix
  must make, such as status code, error shape, what an empty input means, and
  which layer validates, and marks each as decided by the report, decided by
  codebase convention (naming the precedent file), or open.

An **open** decision becomes a question to the reporter or a maintainer, with
the options and the factory's default. The run parks until it is answered, or
proceeds on the convention default after a timeout. The answer becomes a test.

The spec stage is the one place judgment is concentrated, and it is judgment
about *what should happen*, which a person can answer in a sentence, not about
whether code is correct.

### 3. Fix ×N

Run N independent fix agents (default 3), each in its own clone, from the same
frozen spec. Each has the inner loop it has today: the sandbox check running
targeted tests and the typecheck. Candidates are independent: no shared memory,
different seeds, optionally different models.

### 4. Judge (deterministic)

Each candidate goes through host-run checks, cheapest first; a failure stops
that candidate:

1. **Spec tests:** reproduction green twice, preservation green twice.
2. **Compile/typecheck:** the profile's `compile_cmd`.
3. **Affected tests:** the test files that import the changed modules, found
   from the import graph (vitest's `--changed`/related selection), not by name
   matching as today.
4. **Full suite versus baseline:** the whole suite (about 25 minutes today) must
   fail no test beyond the pinned base's known-failing set (32 files today),
   with each new failure confirmed on a rerun to rule out flakes.
5. **Behavior diff:** characterization tests generated against the *pre-fix*
   code of every touched function, route and exported symbol. These are many
   inputs, including empty, missing, boundary and malformed values, with the
   outputs recorded. They are re-run against the candidate, and the host lists
   every input whose output changed.

The behavior diff is what the preservation tests approximate, made complete and
mechanical. It turns "is this change safe?" into a list the host can compare
with the spec: every changed behavior must be one the spec's acceptance tests
ask for. An unrequested change to an input that worked is a rejection, and the
input goes back to the fixer as a failing test.

### 5. Select

Among the candidates that pass the judge:

- Group them by behavior-diff signature. Candidates that change the same
  behaviors agree.
- Pick the largest agreeing group, and within it the smallest diff.
- **Confidence** comes from how many candidates independently reach the same
  behavior, whether any spec decision was taken on a convention default rather
  than stated, and the risk tier of the touched paths.

One reviewer agent reads the chosen candidate for what tests cannot see:
readability, convention, leftover debug code. Its findings are advisory, unless
it names a concrete input that misbehaves. That input becomes a test and goes
back through the judge.

### 6. Propose

Open the PR with its evidence: the spec, the answered questions, the tests
added, the behavior diff, the suite result, and the candidate agreement. The
reproduction and preservation tests land with the fix, so every fixed bug grows
the regression suite. The existing propose design (a push-user fork plus an
App-opened PR) is unchanged. The PR then meets the normal PR pipeline and the
merge click.

## The product test suite

The factory is only as trustworthy as the suite that judges it. Three
investments pay off directly:

- **Baseline health.** The 32 files failing on main should be fixed or
  quarantined with an owner. Every known failure is a blind spot in step 4.
- **Fast selection.** Import-graph test selection keeps the fixers' inner loop
  in seconds, not the full suite's minutes.
- **Growth by construction.** Every fix lands its reproduction and preservation
  tests, and characterization tests that proved useful can be kept.

## Evaluation

The evaluation set (`pipeline/evals/eval_set.py`,
`pipeline/evals/data/issue_fix_eval_set.json`) is the factory's own regression
suite. It holds past bugs, each with its report, its pre-fix tree on a held era
base, and the maintainers' fix and tests as a hidden oracle.

**Per bug, per pass, the scorecard records:**

| measure | meaning |
|---|---|
| proposed | reached step 6 |
| correct | proposed, the hidden oracle passes (or fails only on the maintainers' own contract choices, per `oracle_contract`), and no unrequested behavior change |
| asked | asked a question; scored against whether the report was really ambiguous |
| declined | gave up or routed out, with the reason |
| cost | agent runs, wall time |

**Headline numbers:** *precision* = correct ÷ proposed, which earns autonomy,
and *coverage* = proposed ÷ bugs, which is the value delivered.

**Cadence:** a quick check (10 bugs × 1 pass, about an hour) after small
changes, and the full set (every bug × 3 passes, overnight) for decisions. No
change ships on an anecdote.

**Autonomy bar (proposed):** the factory may propose unattended once precision
holds at 90% or better over at least 30 proposals on the full set.

## Build order

Each step is measured against the one before on the full set.

1. **Evaluation set.** Built in progress. The goal is about 40 fair bugs; the
   Paperclip corpus alone looks like about 25–30, and a public TypeScript
   benchmark subset is the fallback.
2. **Baselines:** the current lane as it is, and a plain single-agent loop with
   the suite as its check, to learn what the multi-stage lane buys.
3. **Deterministic judge:** full suite versus baseline, import-graph selection.
4. **Fix ×N and select.**
5. **Behavior diff:** characterization tests and diff-versus-spec.
6. **Spec stage with the ambiguity check and questions.** Replayed bugs cannot
   receive answers, so the evaluation scores whether the question was the right
   one to ask, with the maintainers' fix standing in as the answer.
7. **Intake.**
8. **Propose,** once the autonomy bar holds.

## Costs

- **Per bug:** about 1 intake + 1 spec + N fixers + 1 reviewer, so about 6–8 agent
  runs at N=3, plus sandbox time dominated by the full-suite run per surviving
  candidate. A handful of bugs a day fits a Max subscription.
- **Full evaluation:** at about 40 bugs × 3 passes, it is the expensive part.
  It runs overnight on the Studio, and the quick check covers day-to-day
  changes.
- **Disk:** about 5–6 GB per held era base, around 50 GB for the set, on
  Colima's 150 GB disk.

## Open decisions

- **Question channel.** Asking needs an upstream write: an issue comment as the
  bot, through a gated, logged executor path like the existing ones. Who else
  may answer: any maintainer, or the reporter only?
- **Timeout** before a convention default applies to an unanswered question.
- **N,** and whether candidates vary the model.
- **Full-suite budget** per candidate: every survivor, or only the selected one
  plus the runner-up.
- **The autonomy bar's numbers.**
