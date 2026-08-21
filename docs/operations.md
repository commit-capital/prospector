# Operations — who runs what

Two kinds of operations: ones **you** run (a single self-contained command, or
an app button), and ones **the agent** runs (an agent orchestrating a
multi-stage phase). The dividing line is fan-out: a job that targets one PR or
one cluster, or that's pure deterministic bookkeeping, is a command you run; a
job that summarizes/clusters/analyzes the whole corpus interleaves an agentic
`workflows/*.js` step and is agent-driven.

## Operations you run

All from the repo root. The first three are deterministic and cheap; the last
three spawn a single headless agent (they spend metered tokens, but are still
one command you invoke). Most are also buttons on the app **Control** tab.

```bash
# refresh open PRs + issue links into the store (read-only gh; cheap)
uv run prospector ingest            # [--max N]

# deterministic threat scan over cached diffs (no agents, no metered tokens)
uv run prospector threat-scan       # [--only 123,456]

# regenerate STATUS.md from the store
uv run prospector status

# one cluster: refresh member facts from GitHub + re-classify dispositions/outcome
uv run prospector triage-cluster --cluster <cid>

# one cluster: re-summarize + re-cluster its members (split a mis-clustered PR out)
uv run prospector recluster --cluster <cid>

# one PR: 3-lens adversarial security review (also the ↻ Run button in the app)
uv run prospector security-review --pr <n>
```

Triage itself is an app operation: open the Clusters board, approve a plan,
and the executor acts upstream as the configured bot (gated, logged). You
never hand-run `gh pr merge/close/comment` against the triaged repo.

## Operations the agent runs

Full **CLUSTER**, **ANALYZE**, and **SECURITY** waves interleave deterministic
drivers with the Workflow scripts in `pipeline/workflows/`. **VERIFY** instead
runs per PR through the app queue and `verify_pr.py` worker. See
`pipeline/workflows/README.md` for the current sequences.

Deterministic drivers own selection, validation, and store writes. Agentic
judgment runs through schema-validated batch Workflows or locked-down per-PR
agents; agents never write the store directly.

## Vocabulary

- **Disposition** (per PR): `merge`, `request-changes` (ask the author for specific fixes, then merge), `close-dup`, `close-fixed`, `close-stale`, `needs-human`.
- **Cluster state**: `needs-analysis`, `awaiting-authors`, `needs-first-party-work` (the feature is wanted but no contributed PR is cleanly salvageable — write our own), `blocked-on-decision`, `security-pending`, `ready`, `done`.

The automatic merge recommendation is strict: **the configured review-provider
bar + passing CI + mergeability + current GREEN security + an author-shipped
verified fix**. Human-initiated merges use `gates.merge_eligibility`: they must
pass every check that actually ran, but missing or inconclusive SECURITY/VERIFY
evidence is not itself a block. The provider and score threshold are deployment
configuration (`TRIAGE_REVIEW_PROVIDER`, `TRIAGE_REVIEW_THRESHOLD`); `none`
disables the external-review requirement.

## Deployment boundary

This is a **local, single-operator tool**. The backend has no authentication
and binds localhost only; the frontend talks to it over the local port.
Running it on a shared host or exposing the ports to a network is unsupported
— anyone who can reach the backend port can read the store and, on a keyed
machine, execute upstream writes.

The security boundary is the machine, not the app:

- **Reads** run as your local `gh` login.
- **Writes** run as the GitHub App, and only on a machine holding the app's private key (`TRIAGE_BOT_KEY_FILE`). No readable key ⇒ the executor mints no token and every write is forced to dry-run.
- Executor writes use the configured GitHub App, refuse an empty bot token, and
  are appended to the activity log; merges additionally pass the per-PR gate in
  `pipeline/gates.py`. Confirmed bot writes from the optional chat agent use its
  own explicit command allowlist; issue closes route through the executor and are
  recorded in that activity log, while its other bot writes are not. Resubmit
  pushes and branch updates run as the operator and append best-effort activity
  entries; configured feedback-repository issue filing also runs as the operator
  and is not recorded there.

## Safety

`CLAUDE.md` is authoritative. In short: reads run as the local login. The app
executor writes as the configured app, logs every attempt, and applies the
per-PR gate to merges. The optional chat agent has a separate,
confirmation-based allowlist for non-merge writes; its issue closes use the
executor and activity log, while its other bot-authenticated writes do not.
Operator-identity resubmits append best-effort entries. With no readable
`TRIAGE_BOT_KEY_FILE`, neither path can write as the app, though local helpers
and configured feedback-repository issue filing remain available. Don't
hand-run `gh pr merge/close/comment` against the configured upstream; use the
app's controlled paths.
