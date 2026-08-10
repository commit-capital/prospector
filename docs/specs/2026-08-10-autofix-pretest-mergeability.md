# Autofix pre-test — proving mergeability without pushing

`TRIAGE_FIX_AUTOHUNT=1` today queues mechanical autofix actions on an idle
worker, and `update` pushes the moment its merge lands: `run_one` returns
through `_finish_pushed` before ever reading `TRIAGE_FIX_AUTOPUSH`. So the
hunter cannot be turned on in a mode that touches nothing.

This makes the hunter a *prover*. It runs each candidate's merge or rebase in
the sandbox, records whether the result is resolvable and compiles, and pushes
nothing. An operator browses the proven set and pushes the ones they want.

## The hunt bar — `gates.fix_huntable`

`gates.py` is the one policy module, so the bar lives there rather than in the
worker:

```python
def fix_huntable(pr: Pr, action: str,
                 changed_paths: list[str] | None = None) -> tuple[bool, str]
```

It requires, in order:

- `is_current(pr, "signals")` — the review-provider bar is unknowable on stale
  signals, which is why `pr_clean` consults `clean_blocker` only inside its own
  freshness branch.
- `review_policy.active().clean_blocker(pr) is None` — the configured provider's
  quality bar is met.
- `fix_eligibility(pr, action, changed_paths)` — the existing fail-closed blocks:
  a malicious threat verdict, any recorded RED security verdict current or
  stale, a profile `autofix.deny_globs` path, a PR that is not open.

It deliberately requires none of `pr.mergeable`, `ci == "passing"`, or a current
GREEN security verdict. Those describe a PR that does not need updating, and
`security_eligible` gates the security review behind `pr_clean` — which requires
`mergeable` — so a conflicted PR never earns a verdict to be GREEN in the first
place. Demanding one would leave the hunt pool permanently empty.

The bar governs the hunter alone. An operator's click still answers to
`fix_eligibility` and nothing more.

`fix_worker.auto_fixable` keeps choosing which action applies — `rebase` when
GitHub reports the PR unmergeable, `update` when the drift scan says the base
moved — and asks `fix_huntable` for the yes/no.

## The probe

A probe is the action's existing mechanics with the push removed.

`rebase` already has one: `run_one` runs `prepare --rebase`, reads `state` to
tell a finished rebase from one paused on conflicts, takes `diff`, and runs the
compile preflight. Only the disposal changes — the prepared worktree is dropped
at park time rather than held.

`update` needs `cmd_update --probe`: the existing clone → fetch → merge body,
stopping before the push. The existing `--dry-run` cannot serve — it returns
before cloning, so it proves nothing about whether the merge applies. Exit codes
already carry the verdict: 8 = the base conflicts, 6 = the head moved, 4 =
git/network, 0 with `after == before` = already current.

Both actions then run `compile_preflight.run_for_patch` over the resulting tree.
This is new for `update`, which skips the preflight today on the reasoning that
a base merge authors no content. That reasoning holds for authorship and not for
correctness: a textually clean merge can still break the build through semantic
conflict, and whether the merged tree compiles is the substance of the claim
being made. It costs one container boot per hunted PR.

## What is recorded

The existing `awaiting-review` status, `source: "auto"`, with `result` carrying:

- `verdict` — `resolvable`, `conflicts`, or `already-current`
- `conflicts` — the conflicted paths, when the merge or rebase stopped on them
- `patch` — the diff, truncated to `TAIL_CHARS` as today
- `compile_preflight` — the preflight record
- `message` — the commit message the push would use

`base_sha` pins the base the probe was proven against. It is already a
first-class parameter on `record_fix_request`, so this needs no schema change
and no `STORE_SCHEMA_VERSION` bump.

A probe that ends `conflicts` lands `refused` with the conflicted paths, not
`awaiting-review` — a PR whose conflicts a machine cannot resolve is not a
candidate for a push button.

## Approving

`push_approved` re-probes against current `main`, then pushes from the worktree
that probe just prepared. Uniform across both mechanical actions.

Re-proving rather than holding a worktree is what keeps a browsable backlog
honest: `approve_pr` already refuses when the *PR's* head moved, but nothing
catches `main` moving underneath a week-old result, and worktrees held for a
backlog of dozens accumulate on the sandbox machine. If the re-probe conflicts,
the request lands `refused` carrying the paths and nothing is pushed.

`fix` is exempt. An agent-authored change is not reproducible, so re-deriving it
at approve time would push something the operator never reviewed. It keeps
today's path: the worktree is held and the reviewed patch is pushed verbatim.

## The `update` push gate

`run_one`'s `update` branch stops returning through `_finish_pushed` and joins
the same park-unless-named-in-`TRIAGE_FIX_AUTOPUSH` path the other actions take.
`TRIAGE_FIX_AUTOPUSH=update` restores the current behavior for a deployment that
wants it.

## UI

Two surfaces:

- **PR detail** — `FixPanel`'s `awaiting-review` branch renders the verdict:
  "Conflicts resolvable" for a clean probe, alongside the preflight result and
  the existing Approve / Discard controls. It names the base the result was
  proven against and says that pushing re-runs the action against current base.

  It does *not* compute whether that base is now behind: the panel polls, and
  resolving upstream's default-branch HEAD is a network round trip per poll. The
  approve path re-proves unconditionally, so a staleness badge would buy
  presentation rather than safety.
- **Fix queue section** — a cross-PR list on the Control tab beside the Verify
  queue, `awaiting-review` rows first since they are the only actionable ones,
  each with Push and Discard. This is the surface that makes a proven backlog
  browsable; it does not exist today. Approving several is a sequence of the
  same per-row approve, so the worker still pushes one PR at a time.

  A row's `resolvable` claim is "the action produced a change and the compile
  preflight did not reject it". A deployment configuring no `verify.compile_cmd`
  records None, which reads as resolvable — the merge resolving is the claim
  being made, and the build check corroborates it.

## Testing

- `gates.fix_huntable` — one case per bar: stale signals, review bar unmet, each
  `fix_eligibility` block, and the allow path. Explicitly: a PR that is
  unmergeable and CI-red but meets the review bar is huntable.
- `auto_fixable` — picks `rebase` on unmergeable, `update` on drift conflicts,
  nothing when the bar refuses, and never `fix`.
- `run_one` for `update` — parks as `awaiting-review` and pushes nothing with
  `TRIAGE_FIX_AUTOPUSH` empty; pushes when it names `update`. This is the
  regression that currently has no coverage at all.
- Probe disposal — a parked mechanical request leaves no worktree behind.
- `push_approved` — re-probes before pushing; a re-probe that conflicts lands
  `refused` and pushes nothing; a `fix` request pushes its stored patch without
  re-deriving it.
- `cmd_update --probe` — merges without pushing and reports the verdict.

`prospector_app/backend/tests/test_fix_worker.py` does not exist yet; the worker
is currently covered only through `test_fix_queue.py`. It is added here.
