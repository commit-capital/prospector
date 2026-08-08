# Autofix queue — pushing small fixes to contributor PRs

> Implementation record. Current behavior is defined by
> `prospector_app/backend/fix_queue.py`, `fix_worker.py`, and `pipeline/gates.py`.

Most PRs in the backlog fail their merge gates on merge drift or nitpicks, not
on substance. Today the only way to clear those is to ask the author and wait.
This adds a queue that authors the fix itself and pushes it to the
contributor's branch as a dedicated machine user.

## The identity problem

Pushing to a contributor's fork head branch relies on the PR's "Allow edits from
maintainers", which grants push to **users with push access to the base repo**.
A GitHub App installation token is not a user, so the configured bot App — which
owns comments, merges, and closes — fundamentally cannot make these pushes.
`prospector_app/agent/resubmit` therefore runs as the operator, dropping
`GH_TOKEN` so the push goes out over an ambient SSH key.

That is the gap this design closes. A third identity joins the trust model:

| Identity | Kind | Scope |
| --- | --- | --- |
| `TRIAGE_BOT_LOGIN` | GitHub App | comment / close / review / merge on `TRIAGE_REPO` |
| operator | user PAT | reads |
| `TRIAGE_PUSH_LOGIN` | machine user, SSH key only | git pushes to open PR head refs |

The machine user gets an **SSH key and no API token**. An SSH key can push and
fetch refs and nothing else — it cannot comment, merge, close, read the API, or
change settings. That is the containment boundary, and it is why the account
cannot misbehave in any direction other than git history on branches.

A fine-grained PAT is not an option regardless: fine-grained PATs scope to
repositories owned by the token's resource owner, and a contributor's fork is
owned by the contributor.

## Deployment setup

One-time, by hand, on the machine that runs the worker.

1. Create the machine user. Its own email; enable 2FA; enable both
   **Keep my email addresses private** and **Block command line pushes that
   expose my email** so a misconfigured `user.email` fails loudly.
2. Generate a dedicated passphrase-less ed25519 key and attach the public half
   to that account as an authentication key. A key binds to one GitHub account,
   so it must be fresh.
3. Add the account to `TRIAGE_REPO` as an **outside collaborator with Write**.
   Write is the floor — maintainer-edits unlocks on push access to the base
   repo, and Triage does not include push. Outside collaborator rather than org
   member keeps it off every other repository.
4. Confirm the default branch already blocks what Write would otherwise permit:
   a ruleset carrying `pull_request`, `non_fast_forward`, and `deletion`, whose
   bypass list does not include repository role 3 (write). Rulesets target
   branches and grant bypass to actors; they cannot deny a single account, so
   this is a repo-wide property to **verify**, not a bot-specific fence.
5. Record the account's numeric id — commits are authored as
   `<id>+<login>@users.noreply.github.com`, which carries the login and the id
   and never the real address.

The bot-specific fence is the one this codebase can enforce: the push path
refuses any target ref that is not the open PR's `headRefName` on its head
repository. See *Push path* below.

### Configuration

Deployment identity goes in `.env`:

```
TRIAGE_PUSH_LOGIN=<machine user login>
TRIAGE_PUSH_SSH_KEY_FILE=~/.ssh/<key>
TRIAGE_PUSH_EMAIL=<id>+<login>@users.noreply.github.com
TRIAGE_FIX_WORKER=1
TRIAGE_FIX_AUTOPUSH=
TRIAGE_FIX_AUTOHUNT=
```

All three identity keys unset → contributor-branch pushes are unavailable, the
buttons render disabled with a reason, and nothing falls back to an ambient
identity. This mirrors how a missing bot key forces every upstream write to
dry-run.

Repository *policy* goes in the profile (`TRIAGE_PROFILE`), not `.env`, because
it reuses the risk-tier and CODEOWNERS vocabulary the profile already owns:

```json
"autofix": {
  "deny_globs": ["..."],
  "fixable_gates": ["ci", "review"]
}
```

There is deliberately no risk-tier condition. `pipeline/risktier.py` documents
itself as an ordering and attention signal that no gate consumes, because path
shape is attacker-controlled; making autofix the first policy to gate on it would
break that invariant. `deny_globs` is purpose-built for this and says what it
means. Neither is a security boundary — the boundary is the threat verdict, the
security review, and CODEOWNERS routing, all of which gate autofix independently,
and an autofixed PR still faces `merge_eligibility` unchanged.

## Architecture

### Store — a `fix_request` section

A new per-PR section beside `verify_request`, carrying `status`, `action`,
`queued_at` / `started_at` / `finished_at`, `source`, the pinned `head_sha` and
`base_sha`, the authored `result` (patch, commit message, preflight verdict),
`attempts`, and `error`. Bumps `schema.STORE_SCHEMA_VERSION`.

States: `queued → running → awaiting-review → approved → pushed`, with terminal
`refused`, `failed`, and `cancelled`.

Actions:

- **`update`** — merge the base branch into the head so CI and the review
  provider re-run against current base code. Authors no content.
- **`rebase`** — rebase onto current base to clear a conflict, then force-push
  with a lease pinned to the author's head SHA.
- **`fix`** — an agent authors a small change addressing a failing gate.

### Policy — `gates.fix_eligibility`

`fix_eligibility(pr, action) -> tuple[bool, str]`, beside `merge_eligibility`.
Hard blocks, all fail-closed:

- `threat` verdict of `malicious` (sticky, no staleness exemption)
- security verdict RED
- any changed path that is CODEOWNERS-gated or matches `autofix.deny_globs`
- PR not open
- a `fix` action where the profile names no `autofix.fixable_gates`

The live PR's `maintainerCanModify` grant is deliberately *not* checked here: the
gate answers from stored facts so any app can render the buttons without a
network round-trip, and `resubmit`'s own preflight re-confirms it — along with
the PR's state and pinned head SHA — immediately before the push.

### Queue and worker

`fix_queue.py` — `queue_pr` / `dequeue_pr` / `approve_pr` / `runner_status`,
served by every app instance so a click on any machine reaches the runner.
State lives in the shared store and survives restarts.

`fix_worker.py` — drains only where `TRIAGE_FIX_WORKER=1`, on the sandbox
machine. Heartbeat thread plus drain loop, orphan recovery, oldest-queued
first, an operator-queued request always beating an auto-queued one. A separate
flag from `TRIAGE_VERIFY_WORKER` so either worker can run alone.

At startup the worker refuses to run if `TRIAGE_PUSH_SSH_KEY_FILE` is missing,
group- or world-readable, or resolves under `VERIFY_SCRATCH` — the sandbox
machine runs untrusted contributor code, so the push credential must be
provably outside anything the sandbox can reach.

`update` skips the compile preflight: a conflicting base stops at `git merge` and
pushes nothing, and a post-merge CI failure is honest signal about the PR.
`rebase` and `fix` run `compile_preflight.run_for_patch` over the authored tree —
a sibling of `run_for_merge` that takes the patch instead of fetching it, so a
change that exists only in a local worktree is measured before it reaches the
contributor's branch. Anything but a clean pass lands `refused` with the build
error, pushing nothing.

### Push path

`resubmit` gains `push_env()` beside `operator_env()`, setting
`GIT_SSH_COMMAND="ssh -i <key> -o IdentitiesOnly=yes"` and the git author and
committer from `TRIAGE_PUSH_LOGIN` / `TRIAGE_PUSH_EMAIL`. `IdentitiesOnly=yes`
is load-bearing: without it `ssh-agent` offers whatever key it holds first and
the commit can land under the wrong account.

A guard refuses any push whose target ref is not the open PR's `headRefName` on
its head repository, or whose pinned `head_sha` no longer matches the live PR.

`cmd_update` merges locally over SSH — clone the head branch, merge
`upstream/<base>`, push behind a lease pinned to the author's head — rather than
calling the `update-branch` REST endpoint. The REST call needs an API token the
machine user does not have, and attributes the merge to whoever triggered it.
Merging locally also sidesteps the `workflow` OAuth scope entirely, since SSH
pushes are not subject to it. It runs on the worker like every other action,
since it now needs a clone.

### Autonomy

`TRIAGE_FIX_AUTOPUSH` is a comma-separated list of actions that skip
`awaiting-review` on a clean preflight; empty by default. `TRIAGE_FIX_AUTOHUNT=1`
lets an idle worker queue eligible PRs itself, as verify autohunt does — but only
the mechanical actions: `rebase` for a PR GitHub reports unmergeable, `update`
for one whose drift scan says the base moved. An agent-authored `fix` is always
an operator's call. Together these are the unattended backlog burn-down, reached
by configuration rather than a rewrite.

### UI and audit

A Fix panel on the PR detail carrying the three actions, each disabled with a
stated reason when ineligible or unconfigured; a queue-status chip; a diff
review view with Approve and Discard; and a Fix Queue tab beside the Verify
queue. Every queue, refusal, approval, and push appends to the activity log
under the machine user's identity.

## Testing

- `gates.fix_eligibility` — one case per hard block, plus the allow path.
- Queue transitions, including double-queue refusal and cancel-after-start.
- Push guard — refuses a ref that is not the PR head; refuses a moved head.
- `push_env` — asserts `IdentitiesOnly=yes` and the configured author identity.
- Worker startup — refuses a world-readable key and a key under the scratch root.
- Profile parse — `autofix` block validation, strict on unknown fields.
