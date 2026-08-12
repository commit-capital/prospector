# Agent-attempted merge-conflict resolution

## Problem

The "Resolve merge conflicts" button queues a mechanical `rebase` autofix
action. When git pauses the rebase on conflicts, the worker aborts and refuses
with "resolving that needs a person." GitHub only marks a PR `CONFLICTING`
when git cannot combine the changes, so for genuine both-sides-edited
conflicts the mechanical path refuses every time. Many such conflicts (e.g.
both sides inserting independent test blocks at the same anchor) have an
unambiguous resolution an agent can author.

## Decisions (settled with the operator)

- **Trigger:** automatic fallback inside the existing button. One click, one
  queue entry; the agent step happens only after the mechanical rebase pauses.
- **Result shape:** a merge commit — merge current base into the PR head and
  resolve inside that merge commit. No history rewrite, normal push.
- **Gating:** no profile opt-in (every resolution parks for explicit operator
  approval), but the agent stays off any PR whose conflicted paths are
  CODEOWNERS-gated or match `autofix.deny_globs`. Autohunt-queued rebases keep
  today's refusal — agent time is only spent on operator-clicked requests.
- **Bar before parking:** the deterministic compile preflight, plus a
  per-file rationale from the agent shown at review time.
- **Where it runs:** the fix worker (the sandbox-capable machine), with the
  agentic half in `pipeline/` invoked through `pipeline/headless_agent.py` —
  the same worker/queue/agent split the verify queue uses.

## Flow

1. Operator clicks "Resolve merge conflicts" → queues action `rebase`
   (unchanged). The fix worker drains it and probes the mechanical rebase.
2. Mechanical rebase completes → parks as the mechanical result (unchanged).
3. Mechanical rebase pauses on conflicts →
   - capture conflicted paths + conflict diff (as today), abort the rebase;
   - refuse (today's message + reason) if the request came from the autohunt,
     or if any conflicted path is CODEOWNERS-gated or deny-globbed;
   - `resubmit <pr> prepare --merge`: merge `origin/<base>` into the head in
     the isolated clone, leave the conflicted merge paused, print conflicted
     paths;
   - run the resolution agent (`pipeline/resolve_conflicts.py`) in that
     worktree: resolve only the conflicted paths, emit JSON
     `{"resolutions": [{"path", "rationale"}]}` or `{"give_up": reason}`;
   - validate fail-closed that only conflicted paths changed; commit the
     merge (`resubmit continue`); `resubmit diff` → patch; compile preflight;
   - park as `awaiting-review` with action `resolve`, keeping the worktree
     (an agent resolution is not mechanically re-derivable, like `fix`).
4. Approve in the UI → `push_approved` pushes the kept tree as the machine
   user: a normal push, no force, refusing if the contributor moved the head
   since prepare. Reject → cancelled, worktree discarded, nothing pushed.

## Components

- **`prospector_app/agent/resubmit`**: `prepare --merge` mode (partial clone +
  `git merge origin/<base>`, pause on conflict), a merge-mode arm for
  `continue` (stage resolved paths, commit the merge), `diff` over the merge
  result, and a merge-mode `push` with no `--confirm-rewrite`. Existing
  guards (`assert_push_target`, head-unmoved-since-prepare, open-PR-only)
  apply unchanged.
- **`pipeline/headless_agent.py`**: an opt-in editing mode — `Edit`/`Write`
  permission-scoped to the worktree path, read-only git in the worktree, no
  gh, no network tools, `--safe-mode`.
- **`pipeline/resolve_conflicts.py`** (new): drives the agent over a prepared
  merge worktree; owns the prompt template (conflict-marked files, PR
  title/body, both sides' recent commit subjects; prefer preserving both
  intents; give up rather than guess).
- **`pipeline/settings.py` / `pipeline/schema.py`**: `resolve` joins
  `FIX_ACTIONS`; `STORE_SCHEMA_VERSION` bumps (older writers must not
  mishandle the new action value).
- **`pipeline/gates.py`**: `fix_eligibility` knows `resolve` — CODEOWNERS
  block on the conflicted paths, no `fixable_gates` requirement.
- **`prospector_app/backend/fix_worker.py`**: the fallback branch after a
  paused rebase; parking as `resolve` keeps the worktree; `push_approved`
  treats `resolve` like `fix` (push the kept tree).
- **Frontend**: FixPanel labels a parked `resolve` "Agent-resolved conflicts —
  review & approve"; PR detail shows conflict hunks, the resolved patch, the
  per-file rationale, and the preflight result next to the existing
  approve/reject controls; running requests show a step indicator.

## Parked record

The `resolve` result carries: patch tail, compile preflight, per-file
rationale, conflicted paths, the pre-resolution conflict hunks (merge diff),
and the base/head SHAs it was proven against.

## Failure handling

Agent timeout, error, give-up, or an out-of-scope edit → abort the worktree
and refuse, appending the agent's reason to today's message. Preflight
failure → refuse with the preflight evidence attached. Every exit writes a
terminal status; no request is left `running`.

## Testing

- resubmit merge-mode state machine against local fixture repos;
- fix-worker fallback branching with the agent mocked;
- gates: CODEOWNERS-on-conflicted-paths, deny_globs, autohunt carve-out;
- `push_approved` keeps-tree path for `resolve`;
- store round-trip of the new action value.
- Gates: pyright 0 errors, ruff clean, pytest all suites, frontend `tsc`.
