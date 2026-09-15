# Objections become fix goals

Date: 2026-09-14. Draft for review.

## Problem

Every agent-authored change that a reviewer does not clear is parked for the
operator. On 2026-09-14 the resolve auto-review rejected six of nine
resolutions, each with a concrete, actionable reason ("the merge keeps the
PR's tests that assert X, which the base's #11626 now prevents"). Two more
were held only by the risk tier. 225 open PRs carry a YELLOW security
verdict whose finding names the defect. A rebase that lands cleanly can still
fail the compile preflight on a real type error the excerpt names. In each
case an agent has already written the goal, and the system's only move is to
put the PR in front of a human.

## Goal

When a machine judgment names a specific defect in a change the agents made
or a PR the agents could fix, hand that judgment to the authoring agent as
the goal, re-judge the result under the same bar, and only then park or
push. A human sees the outcome, not the intermediate step.

## Sources of objections

| Source | Objection text | Runs on |
|---|---|---|
| Resolve auto-review: a behavior- or history-lens reviewer returns `unsafe` with a reason | reviewer `reason` + `concerns` | the resolve's kept merge worktree |
| Compile preflight exit 20 on a resolve or update where the base passes | `error_excerpt` | the resolve's kept worktree, or a fresh head clone for an update |
| Fix review (`review_fix`) returns `unsafe` on an authored fix | reviewer reason | the fix's worktree, one retry |
| Security YELLOW verdict, current at the head | each confirmed finding's title and detail | a head clone (the PR must be mergeable) |

Not sources: RED security (human by policy), the resolving agent's own
`give_up` (it declined for lack of knowledge; a second agent has no more),
Greptile below-bar (already the unguided `fix` hunt's job), and any
reviewer failure that is the machine's (a crashed reviewer restores and
retries; it is not an objection).

## Trust model

- An objection fix is a new fix_request source, `objection`, distinct from
  the operator's `guidance`. It never counts as human authorization:
  `gates.fix_eligibility` treats it as unguided, so the profile must name
  `objection` in `autofix.fixable_gates` for it to run at all. CODEOWNERS,
  `deny_globs`, tier-0 paths, malicious threat and RED security block it
  exactly as they block every fix.
- The result is judged under the bar of the action it continues: a resolve
  continuation faces `resolve_autopush_bar` (two lenses, related tests, tier);
  a fix continuation faces `review_fix` plus the compile preflight. Passing
  parks it unless `TRIAGE_FIX_AUTOPUSH` names the action.
- One objection fix per (PR, head, objection signature). A continuation the
  reviewer rejects again parks with both objections shown and spawns nothing.
  A hunt slot is held from the objection until the continuation ends.
- Daily budget: `TRIAGE_FIX_OBJECTION_BUDGET` (default 20) continuations per
  worker per UTC day, with the count in the heartbeat and the Control tab.

## Mechanics

**Resolve continuation.** `_judge_claimed_resolve` today ends with
`awaiting-review` when a lens rejects. Instead, when the deployment opts in
and the rejection is judged (not a machine failure): keep the worktree,
record the request `running` with step `continuing after review`, run
`author_fix.author` in the merge worktree with goal = the objection, findings
= the reviewer's `concerns`, the merge diff as context, and the sandbox check
available. Hold the patch to the files the agent reported and re-gate the
touched paths. Then re-run both review lenses over the combined diff, the
related tests, and the compile preflight, and stamp a second `auto_review`
entry (`auto_review.rounds`, newest last). Pass → `approved` (autopush path)
or `awaiting-review`. Fail → `awaiting-review` with both rounds.

**Compile continuation.** In `_end_on_preflight`, an exit 20 whose base
passes (`base_fails` absent) on a hunted resolve or update becomes an
objection with the excerpt as goal; the worktree is kept for a resolve, or
re-prepared for an update. The continuation is re-preflighted; there is no
reviewer for an update, so the compile pass is the bar and it parks unless
`update` is in autopush.

**Fix retry.** `_author_fix` on a `review_fix` rejection runs the author once
more with the rejection appended to the goal, then reviews again. Two
rejections park.

**Security continuation.** A new hunt lane, behind `TRIAGE_FIX_HUNT_SECURITY=1`:
a mergeable, CI-green PR with a current YELLOW verdict is queued as a `fix`
with source `objection` and goal = the findings. On push the head moves, the
verdict goes stale, and the security hunter re-reviews. The bar is
`review_fix` plus compile; the fix parks unless `fix` is in autopush.

## Surfaces

- `fix_request.objection`: `{source, signature, text, from: {lens | finding | preflight}}`.
- Ledger `fix:single` entries carry `source=objection` and the round count.
- The fix queue row shows "continued after review: <one line>" and both
  rounds' verdicts; the Control tab summary counts continuations and their
  endings.

## Rollout

1. Resolve continuation and compile continuation, parked only.
2. Fix retry.
3. Security continuation behind its flag.
4. Once the parked continuations show a pass rate worth trusting, the
   deployment names `fix` and `resolve` in `TRIAGE_FIX_AUTOPUSH`, which is
   where the operator's clicks actually go away.

## Open decisions for the operator

- Turn on `objection` in `autofix.fixable_gates` for the deployment's profile.
- Whether `fix` joins `TRIAGE_FIX_AUTOPUSH` at step 4, and under what extra
  condition (tier ≥ 2, diff under N lines, both reviewers).
- The daily budget.
