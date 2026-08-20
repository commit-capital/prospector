# Autofix hunting for agent-authored fixes — design

Date: 2026-08-20
Status: approved by operator (chat), implementing

## Goal

Move the request-changes backlog toward the "PRs ready to merge" bucket without
per-PR operator clicks. Today (live store, 2026-08-20): 2,053 open
request-changes PRs; 381 of them are CI-passing and mergeable (the Home card),
254 of those are verbatim ANALYZE merge picks demoted by the review bar or a
verify/security fact. 377 of the 381 fail `fix_eligibility("fix")` for exactly
one reason — the profile names no `autofix.fixable_gates`. Separately, 651
conflicted/stale request-changes PRs already pass `fix_huntable("update")` and
only wait on `TRIAGE_FIX_AUTOHUNT=1`.

The human touchpoint moves to the merge decision: agent fixes push unattended
once the existing safeguards pass (refuting reviewer, disclosed-files check,
path re-gate, compile preflight), and `merge_eligibility` is untouched — a
human still clicks every merge.

## Operator decisions (recorded)

1. **Human gate: at merge only.** `TRIAGE_FIX_AUTOPUSH` includes `fix`.
   `rebase` stays parked (it force-pushes over contributor history).
2. **Queueing: the hunter auto-queues `fix`**, behind its own env opt-in
   (`TRIAGE_FIX_HUNT_FIX=1`), reversing the "an agent-authored fix is an
   operator's call" policy for this deployment explicitly.
3. **Deny globs: CI/workflows, agent instruction files, dependency manifests.**

## 1. Configuration (no code)

`profile.json` gains:

```json
"autofix": {
  "fixable_gates": ["review", "ci"],
  "deny_globs": [
    ".github/**",
    "CLAUDE.md", "**/CLAUDE.md",
    "AGENTS.md", "**/AGENTS.md",
    ".claude/**",
    "package.json", "**/package.json",
    "package-lock.json", "pnpm-lock.yaml", "pnpm-workspace.yaml",
    "yarn.lock", "bun.lock", "bun.lockb"
  ]
}
```

(The manifest globs mirror the Node-shaped subset of
`profile.GENERIC_DEPENDENCY_MANIFESTS` that applies to the target repository.)

Worker machine `.env`:

```
TRIAGE_FIX_AUTOHUNT=1
TRIAGE_FIX_HUNT_FIX=1
TRIAGE_FIX_AUTOPUSH=update,fix
```

## 2. Settings (`pipeline/settings.py`)

- `fix_hunt_fix() -> bool`: `TRIAGE_FIX_HUNT_FIX == "1"`, read live like the
  other three worker switches.
- `FIX_HUNT_LIMIT` (`TRIAGE_FIX_HUNT_LIMIT`, default 3): the most auto-queued
  `fix` requests allowed in flight at once. Mechanical actions are not capped.

## 3. Gate (`pipeline/gates.py`)

`HUNTABLE_ACTIONS` grows to `("update", "rebase", "fix")`, and `fix_huntable`
gets per-action logic:

- `update`/`rebase`: unchanged — signals current, review bar **passes**
  (`clean_blocker` is None), then `fix_eligibility`.
- `fix`: signals current; CI `passing`; `mergeable is True`; and the PR
  actually has a fixable gate failing: review score **below** the bar with the
  score **not stale** (a stale score means the right move is a re-review, not
  a fix), or — when the profile names `ci` — CI failing (moot while the CI
  clause above requires passing; the disjunction is written so the CI arm
  activates if the entry conditions are later widened). Then
  `fix_eligibility(pr, "fix", changed_paths, guided=False)` — profile opt-in,
  threat/RED, CODEOWNERS, deny-globs all unchanged.

The docstring's "an agent-authored fix is an operator's call" rationale moves
to: the hunter queues `fix` only where the deployment opted in twice (profile
`fixable_gates` + `TRIAGE_FIX_HUNT_FIX`).

Whether the *worker* hunts `fix` at all is the worker's check
(settings-driven), not the gate's — `fix_huntable` stays a pure policy
function over stored facts.

## 4. Worker (`prospector_app/backend/fix_worker.py`)

`auto_fixable(pr)`:

- keeps the mechanical arms (unmergeable → `rebase`, drift conflicts →
  `update`);
- adds: else, when `settings.fix_hunt_fix()` and the auto-queued-fix in-flight
  count is below `FIX_HUNT_LIMIT`, try `fix` via
  `gates.fix_huntable(pr, "fix", changed_paths)`.
- **One attempt per head SHA:** skip `fix` when the PR's existing
  `fix_request` is a terminal record (`pushed`, `failed`, `refused`,
  `cancelled`) whose recorded head stamp equals the current `pr.head_sha` and
  whose action was `fix`. `_finish_pushed`, `_settle`, and the refusal path
  must carry the head stamp forward so this guard has something to read. A
  moved head re-arms the PR.

`next_auto()` ordering (today: ascending PR number): hunt picks are ordered
mechanical actions first (cheap, unblock the most), then `fix` by review
severity tier — `nits`, then score 4, then 3 and below — and community pain
descending within a tier.

## 5. Closing the loop after a fix push

After `_finish_pushed` for a `fix` (and only a `fix` — an `update`'s merge
commit does not need a fresh review to clear a bar it already passes):

1. Post the review provider's retrigger mention as the bot via
   `executor.retrigger_greptile(n, token=executor.mint_bot_token(), dry_run=False)`
   — the existing curated, Activity-logged path. This is the one new
   *unattended* bot write this design adds. No token mintable → the retrigger
   is skipped (dry-run forced), logged, and the PR waits for the next
   scheduled ingest; the push itself is unaffected.
2. `review_refresh.capture` before the mention + `review_refresh.schedule`
   after, so the new score, reviewed SHA, and gates land in the shared store
   without a UI session.
3. Targeted ingest refresh of the PR's signals (the push moved the head; CI
   restarts upstream on its own).

Best-effort: a retrigger failure never fails the push record.

## 6. What this does not change

- `merge_eligibility`, `merge_allowed`, and every merge-path gate: untouched.
- No auto-merge. The merge bucket is still human-clicked.
- `resolve` still never autopushes; `rebase` still parks for approval.
- needs-human, close-*, RED, malicious, CODEOWNERS-gated, and deny-glob PRs
  are never auto-fixed.
- The verify/security autohunt already running carries pushed fixes onward.

## 7. Testing

- `fix_huntable` per-action logic: fix requires below-bar + non-stale +
  CI passing + mergeable; update/rebase behavior unchanged.
- Worker: hunt-fix off by default; `FIX_HUNT_LIMIT` respected; one-attempt-
  per-head guard (terminal record same head → skip; head moved → re-arm);
  ordering (mechanical first, nits before 4 before 3, pain desc within tier).
- Retrigger hook: fires only for pushed `fix`; skips cleanly with no token;
  failure leaves the `pushed` record intact.
- Gates: pyright, ruff, full pytest stay clean.

## Expected motion

Day one: ~651 mechanical updates drain unattended; ~100 nits-only fixes lead
the fix queue, then the ~250 demoted merge picks. Each pushed fix re-reviews,
re-runs CI, then flows through the existing security/verify autohunt into
"PRs ready to merge".
