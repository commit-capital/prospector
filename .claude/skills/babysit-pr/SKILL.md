---
name: babysit-pr
description: Use after opening or pushing to a Prospector PR (commit-capital/prospector), or when asked to watch, babysit, or shepherd a PR to green. Drives the PR until every required check passes — waiting on CI without sleeping, fixing real failures, rerunning flakes once, catching up with main — and squash-merges it when the issue it closes is assigned to the operator.
---

# Babysit a Prospector PR to green

The job is not done when the PR is opened. It is done when the PR is green and
either merged (when you are authorized to merge it, below) or handed back with a
plain statement of what is left. Do not go idle on "I'll check CI later"; wait on
CI with a blocking watch and act on the result in the same session.

This skill is for **this repository** (`commit-capital/prospector`) only. It never
applies to `TRIAGE_REPO`: writes there go through the app's sanctioned paths, and
the project hook denies them anyway.

## 0. Identify the PR

```bash
gh pr view --json number,url,headRefName,isDraft,mergeStateStatus,closingIssuesReferences
```

Run it from the PR's worktree (or pass the number). Note the number `N`.

## 1. Wait for CI — blocking, never a sleep

```bash
gh pr checks N --required --watch --fail-fast --interval 30
```

Run it with the Bash tool's `run_in_background: true`. You are re-invoked when it
exits, so there is nothing to poll and nothing to schedule. A full run takes
roughly 10–15 minutes. If checks have not registered yet ("no checks reported"),
wait for them with a background `until gh pr checks N >/dev/null 2>&1; do sleep 15; done`
and then start the watch.

The required checks (ruleset on `main`, strict — the branch must be up to date):
`pytest`, `pyright`, `ruff`, `frontend-build`, `fresh-install`, `release-guard`.
CodeQL `Analyze (*)` runs too but is not required; still read any new alert it
raises on your diff.

## 2. Act on the result

Loop steps 1–2 until green. Cap it at **five fix pushes**; past that, stop and
report (step 4) instead of thrashing.

**All green** → step 3.

**A check failed** → read the failure before touching code:

```bash
gh pr checks N
gh run view <run-id> --log-failed | tail -200
```

Then classify it:

- **Real failure caused by the diff** — reproduce it locally with the same
  command CI runs (`uv run pytest <path>`, `uv run pyright pipeline issue_triage
  alert_triage prospector_app/backend review-new-pr/harness`, `uv run ruff check
  .`, or from `prospector_app/frontend/`: `pnpm run build`, `pnpm run lint`,
  `pnpm test`). Fix the root cause (use `superpowers:systematic-debugging` if it
  is not obvious), rerun the local check green, commit, push, back to step 1.
  Never weaken a test, skip a check, or add an ignore to get green.
- **Flake or infrastructure** (runner lost, network timeout, a test that passes
  locally and touches nothing in the diff) — rerun once:
  `gh run rerun <run-id> --failed`, back to step 1. A second failure of the same
  check is treated as real.
- **Pre-existing failure on `main`** (the same check is red on main's latest
  run: `gh run list --branch main --limit 3`) — do not fix unrelated breakage
  inside this PR. Report it and stop.

**Branch behind `main`** (`mergeStateStatus` is `BEHIND`, required because the
ruleset is strict) → merge main in and push, then back to step 1:

```bash
git fetch origin main && git merge --no-edit origin/main
```

A conflict you can resolve with confidence: resolve, run the affected tests,
push. One you cannot: stop and report.

**Pushing.** This machine has `push.default=tracking`, so always push with an
explicit destination:

```bash
git push origin HEAD:refs/heads/<headRefName>
```

Never force-push unless you rebased on purpose, and then only with
`--force-with-lease`.

## 3. Merge — only when authorized

Merge only when **all** of these hold:

1. Every required check is `pass` on the current head, and the PR is not a draft.
2. `mergeStateStatus` is `CLEAN` (not `BEHIND`, `BLOCKED`, `DIRTY`, or `UNSTABLE`).
3. The PR closes at least one issue (`closingIssuesReferences`, i.e. a
   `Fixes #N` line), and **every** closing issue is assigned to the operator —
   the login `gh api user --jq .login` prints (`brandonburr`). Check with
   `gh issue view <n> --json assignees`.
4. No review is `CHANGES_REQUESTED` and no review comment is unanswered
   (`gh pr view N --json reviews,reviewDecision`).
5. Nothing in this session's conversation told you to hold the PR.

Then:

```bash
gh pr merge N --squash --delete-branch
```

The assignment is the operator's standing authorization for that issue's fix; it
covers this merge and nothing else. If any condition fails — no linked issue,
an issue assigned to someone else or to no one — leave the PR open, green, and
report it as ready for review. Never enable GitHub auto-merge, and never merge
with `--admin`.

After merging, confirm: `gh pr view N --json state,mergeCommit` reads `MERGED`,
and the closing issue reads `CLOSED`.

## 4. Report

End with a short, plain summary for the operator: the PR link, whether it is
merged or waiting (and on what — review, a decision, a pre-existing main
failure, a conflict you could not resolve), and what you fixed along the way.
No internal mechanics beyond what they need to act.
