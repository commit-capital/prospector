# Unattended pushes for agent-resolved merge conflicts (`resolve` autopush)

## Problem

A `resolve` — an agent's resolution of a rebase that paused on real merge
conflicts — always parks as `awaiting-review` and waits for an operator to
click Push. At the current scale (~90 parked resolves, more hunted every day)
the manual gate does not clear: parked resolves go stale as heads move, get
refused on approval, and pile back up. The human review step must become the
exception, not the rule.

## Decision summary (agreed with the operator)

- **Opt-in**: naming `resolve` in `TRIAGE_FIX_AUTOPUSH` (already a legal value
  — `settings.FIX_ACTIONS` includes it; today it is inert) means "push a
  resolve unattended when the full evidence bar passes". Applies to hunted and
  operator-clicked resolves alike.
- **Evidence bar**: two independent history-aware refuter agents (unanimous
  explicit `safe` required) **plus** a sandbox run of the test files related to
  the conflicted paths (a failing run blocks; having no related tests is
  recorded but does not block).
- **Hard bound**: a resolve whose conflicted paths reach risk tier 0 (the
  profile's highest) always parks for a human, whatever the agents say.
- **Failure mode**: anything short of the full bar leaves the request parked as
  `awaiting-review`, now carrying the recorded verdict so the operator sees
  *why* it needs them. The Fix queue becomes the reject pile.
- **Backlog**: the same lane drains already-parked resolves. One whose head
  moved is cancelled with a reason (the hunter re-arms the rebase for the new
  head as usual); one whose artifact is intact is reviewed and pushed in place.
- **Architecture**: review-at-push-time in the fix worker's drain loop
  (approach A). `_agent_resolve` keeps parking unconditionally.

## Non-goals

- The merge boundary is untouched: an autopushed resolve still faces
  `merge_eligibility` unchanged.
- No change to `fix`/`describe` autopush semantics, to operator approval
  (`approve_pr` stays the human override, no agent bar applied), or to the
  resolve authoring flow itself.
- No live GitHub archaeology: history evidence comes from the merge worktree's
  git history plus what the store already holds.

## Components

### 1. `pipeline/resolve_evidence.py` (new)

Deterministic evidence assembly inside the kept merge worktree (HEAD is the
merge commit; `HEAD^1` = the PR's head, `HEAD^2` = the base that was merged
in). All functions are read-only over git and the store.

- `history(worktree: str, conflict_paths: list[str]) -> str` — for each
  conflicted path: `git log --oneline -n 8` of the path on each side
  (`HEAD^2..HEAD^1` and `HEAD^1..HEAD^2`), so a reviewer sees which commits —
  and, via squash-merge subjects, which PRs/issues — produced each side's
  hunks. Rendered as a compact text block for the prompt; capped per path.
- `store_context(rec: Pr) -> str` — the PR's own stored summary and linked
  issues (titles + one-liners), best-effort.
- `related_tests(worktree: str, conflict_paths: list[str]) -> list[str]` —
  repo-relative test files related to the conflicted paths: any conflicted
  path that is itself a test path (`diffpaths.is_test_path`), plus test files
  in the worktree whose basename stem matches a conflicted file's stem, plus
  test files whose text references that stem (bounded grep), deduped and
  capped (10 files).

### 2. `pipeline/review_resolve.py` (new, sibling of `review_fix.py`)

`review(worktree: str, *, pr: int, title: str, merge_diff: str, patch: str,
resolutions: list[dict], history: str, store_context: str, lens: str,
on_event=None) -> dict` returning
`{"verdict": "safe"|"unsafe", "reason": str, "concerns": [str], "failed"?: bool}`.

Same contract as `review_fix.review`: a fresh headless agent, read-only tools,
`cwd`/`edit_root` scoped to the merge worktree, `allow_gh=False`; only a
well-formed explicit `safe` is safe; a refusal, malformed answer, timeout, or
crash is `unsafe` (with `failed: True` when the machine, not the judgment,
failed). Never raises.

The prompt frames the job as refutation: *find the reason this conflict
resolution must not be pushed*. It carries the conflicted hunks (`merge_diff`),
the resolver's final diff (`patch`), its per-file rationale, the per-side
commit history, and the store context. Two lenses, run as two independent
agents:

- `behavior`: does the resolved code preserve both sides' behavior — is any
  hunk from either side silently dropped, any guard/validation lost, any
  caller or test outside the diff broken?
- `history`: from the per-side commit history and the PR/issue context, what
  was each side *for* — a bug fix, a feature, a revert — and does the
  resolution keep the older change's purpose intact while landing the new
  one? Reason about how the earlier changes would be exercised (their
  repro/tests) and whether that still works.

### 3. `gates.resolve_autopush_bar` (the ONE policy)

```python
def resolve_autopush_bar(rec: Pr, result: dict) -> tuple[bool, str]
```

Judges a parked resolve's recorded evidence (`result["auto_review"]`, shape
below). Pass requires all of:

- tier bound: `risktier.pr_tier(conflict_paths) != 0` (and not `None`);
- two review verdicts, both `safe`;
- test evidence: `tests` record absent-or-empty selection is acceptable;
  a run that exists must have `exit == 0`.

Returns `(False, reason)` naming the first miss. Pure function over the
record, unit-testable, no I/O. (`risktier.py`'s module docstring is updated:
tier is now consumed by this one autopush policy.)

### 4. Fix worker: the review lane (`prospector_app/backend/fix_worker.py`)

Drain-loop priority becomes: approved pushes → queued requests → **parked
resolve review** → autohunt. The lane is active only when `"resolve" in
settings.fix_autopush()`.

`next_reviewable() -> int | None`: the oldest `awaiting-review` request with
`action == "resolve"`, authored by this host (`host` match, like
`next_approved`'s `mine_only`), whose `result` has no `auto_review` stamp yet.

`review_resolve_request(n)`:

1. Reload the record. If the PR's head moved past `against_head_sha`: abort
   the worktree (`resubmit abort`), record `cancelled` with reason "head
   moved before auto-review; the hunter re-arms on the new head", ledger
   entry as usual. (Re-queueing happens through the existing autohunt
   machinery — the new head has no attempt yet.)
2. Locate the kept worktree via `resubmit state`; a missing worktree records
   the same cancellation (the artifact is gone; nothing to judge).
3. Mark the step visible: keep status `awaiting-review` (no new status, no
   store-schema change; only this host's single drain thread touches it) but
   `_running_step`-style progress is NOT used — instead the work-status
   flyout's job label and worker `current_pr` show activity.
4. Assemble evidence (`resolve_evidence`), run the two reviewers, then — only
   if both are `safe` — select `related_tests` and run the profile's test
   runner over them with `compile_preflight.run_command_for_patch` (command
   composed exactly as `sandbox_check.lane_command(["test", …])` does) against
   the parked `patch`. Reviewers first: they are the cheap gate for the
   expensive sandbox.
5. Stamp `result["auto_review"]` and re-record the request (status unchanged):

   ```json
   {
     "against_head_sha": "…", "base_sha": "…", "host": "…", "at": "…",
     "tier": {"tier": 2, "pinned_by": ["…"]},
     "reviews": [{"lens": "behavior", "verdict": "safe", "reason": "…",
                  "concerns": []},
                 {"lens": "history", "verdict": "…", "reason": "…"}],
     "tests": {"files": ["…"], "run": {"exit": 0, "…": "…"}} | null
   }
   ```

6. Run `gates.resolve_autopush_bar`. Pass → record `approved` (result carried
   whole, `source` preserved); the drain loop's next iteration pushes it
   through the existing `push_approved` path (worktree reuse, recheck,
   activity log, ledger, all unchanged). Fail → the request simply stays
   `awaiting-review` with the stamped verdict; the stamp's presence keeps
   `next_reviewable` from re-judging the same artifact. A reviewer that
   `failed` (machine failure, not judgment) leaves no stamp when *neither*
   reviewer reached a verdict, so a recovered machine retries; a single
   failed reviewer stamps `unsafe` (fail-closed).

An operator's manual Push (`approve_pr`) is untouched and overrides any
stamped `unsafe` — the verdict is advice to the human, a gate only for the
machine.

### 5. Flag & docs

- `settings.parse_fix_autopush` already accepts `resolve`; `.env.example`'s
  comment gains it with a sentence on the bar.
- `CLAUDE.md`'s AUTOFIX paragraph: replace "a resolve never autopushes
  regardless of `TRIAGE_FIX_AUTOPUSH`" with the new rule.

### 6. App surface

- **Setup tab** (`Setup.tsx`): the `TRIAGE_FIX_AUTOPUSH` switch becomes two
  entries composing one value: the existing "Push branch updates without
  asking" (`update,rebase`) and a new "Push agent-resolved conflicts after two
  agent reviews pass" which adds/removes `resolve` in the same env value
  (frontend composes the string; `worker_control` is unchanged — the value it
  writes is already validated against `FIX_ACTIONS`). The resolve entry
  `needs: ["push_identity"]` and renders only meaningfully when the base
  autopush entry is on.
- **FixPanel**: an `awaiting-review` resolve with an `auto_review` stamp shows
  the verdict under the banner — pass ("cleared for autopush, pushing
  shortly") or the failing reviewer's reason / tier bound / failing test — so
  the queue reads as "here is why a human is needed".
- **Fix queue view**: a small chip on stamped rows (`agents: cleared` /
  `agents: blocked`).

## Data & compatibility

- Only additive: `fix_request.result.auto_review` (new key) and the existing
  `approved` status now sometimes recorded by the worker itself. No new
  statuses, no mirror columns, no `STORE_SCHEMA_VERSION` bump (older code
  ignores unknown result keys and already handles `approved`).
- Ledger: unchanged shape — the eventual `pushed`/`cancelled` ending carries
  its reason; the auto-approved push is distinguishable by
  `result.auto_review` on the record.

## Security posture

Unattended agent-authored content reaching a contributor's branch is bounded
by, in order: `fix_eligibility` (malicious verdict, RED security, CODEOWNERS,
deny-globs, non-open PR) at authoring time and re-checked at push
(`recheck_eligibility`); the tier-0 park; two independent refuters that must
both affirmatively clear it (fail-closed on any malfunction); the compile
preflight already recorded at park time; the related-tests sandbox run;
`assert_push_target` pinning the push to the open PR's own head ref; and the
unchanged merge gate downstream. The push identity remains the machine user
with its pinned SSH key; nothing new touches tokens.

## Testing

- `pipeline/tests/test_resolve_evidence.py`: fixture git repo with a real
  two-sided merge — history rendering, related-test discovery (test-path
  conflicts, stem matches, grep hits, caps).
- `pipeline/tests/test_review_resolve.py`: mock `headless_agent` — explicit
  safe, unsafe, malformed, timeout ⇒ verdict contract (mirrors
  `test_review_fix`).
- `pipeline/tests/test_gates.py` additions: `resolve_autopush_bar` — tier
  bound, one-unsafe, missing reviews, failing tests, empty-test pass.
- `prospector_app/backend/tests/test_fix_worker.py` additions: review lane —
  picks only this host's unstamped parked resolves; head-moved ⇒ cancelled;
  both-safe ⇒ approved then pushed via existing path; one-unsafe ⇒ stays
  parked with stamp, not re-reviewed; both-reviewers-failed ⇒ no stamp
  (retry); lane inert without the flag.
- Frontend: `pnpm run build` clean; lint on touched files.

## Implementation order

1. `resolve_evidence.py` + tests.
2. `review_resolve.py` + tests.
3. `gates.resolve_autopush_bar` (+ `risktier` docstring touch-up) + tests.
4. Worker lane (`next_reviewable`, `review_resolve_request`, drain hook) +
   tests.
5. Docs: `.env.example`, `CLAUDE.md`.
6. Frontend: Setup toggle, FixPanel verdict, queue chip; build + lint.
