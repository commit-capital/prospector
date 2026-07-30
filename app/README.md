# Prospector web app

A local human+AI review GUI over the repository configured by `TRIAGE_REPO`. It
turns a large open-PR backlog into a fast review surface: a ranked cluster board,
an all-PRs triage queue, an embedded diff viewer with agents' safety findings
rendered **inline**, a live per-line chat with a codebase-expert agent, and
controls to start triage jobs.

The human's job shrinks from re-analyzing PRs to **approving pre-made plans**.

## Safety model (read this)

The cockpit executor performs triage actions directly upstream as the configured
GitHub App — comment, close, reopen, review, and gated merges (see the repo
`CLAUDE.md`). Reads run as the local login. Executor writes are controlled and
audited:

- Every executor shell-out goes through `backend/safety_guard.py`, an allowlist that
  permits only comment/close/reopen/review (as the configured bot) plus the
  dedicated `bot_merge_run` path, and **refuses any write with an empty token**.
  Reads (`gh` reads, `git` reads, `claude -p`, `python`) pass through. Unit-tested
  in `backend/test_safety_guard.py`.
- Writes mint the configured app's token via `get-bot-token.sh`. On any machine
  where no token can be minted, every write is **forced to dry-run** — it
  physically cannot post. Merges are additionally gated per-PR by
  `gates.merge_eligibility`. Every executor write, dry-runs included, is appended
  to the activity log.

### What the "ask the agent" chat is allowed to do

The cockpit's chat agent (the **Ask** pane, `backend/chat.py`) is a *separate*
actor from the executor above. Its permissions come from a narrow command
allowlist:

- **Reads:** repository files, the local store, and read-only GitHub operations
  (`pr view/diff/list/checks`, `issue view/list`, searches, and workflow-run
  inspection).
- **Local writes:** curated helpers may record agent memory, detach a PR from an
  incorrect cluster, and reingest a moved PR through the validated store
  accessor.
- **Upstream writes as the configured bot, after confirmation:** when a bot token
  can be minted, the agent may edit a PR's title/body, comment, close, reopen,
  submit a review, manage issues, and rerun a workflow. It cannot merge.
- **Writes as the operator, after confirmation:** on a machine that can mint the
  bot token, the `resubmit` helper may push an agreed code change to a
  contributor's editable fork branch or update that branch from the base branch.
  Separately, when `PROSPECTOR_FEEDBACK_REPO` is configured, `file-issue` may always
  open a tooling issue there as the operator. Both helpers drop the injected bot
  token before invoking GitHub.

The agent drafts the exact upstream change in chat and acts only after the
operator confirms. With no bot key it has no path that writes to `TRIAGE_REPO`,
but its local helpers and the configured feedback-repository path remain
available. `--permission-mode dontAsk` silently denies every command outside the
allowlist. Bot-authenticated chat writes and feedback issue filing are not
recorded in the executor activity log; resubmit pushes and branch updates append
best-effort activity entries under the operator's identity. The full operating
manual is `app/agent/context.md`.

## Run

```bash
uv run prospector serve --dev   # starts backend :8787 + Vite dev :5173, open http://localhost:5173
```

Running several worktrees at once? Give each its own ports so they don't collide:

```bash
cp .env.example .env   # then set distinct VITE_PORT / API_PORT for this worktree
```

`.env` is gitignored; `prospector serve --dev` and `vite.config.ts` both read it (Vite proxies
`/api` to `API_PORT`, so each frontend always talks to its own backend). Leaving
it unset keeps the defaults above.

Or separately:

```bash
# backend
uv sync                      # from the repo root — builds the locked 3.14.6 env
uv run uvicorn app.backend.app:app --port 8787 --reload
# frontend
pnpm --dir app/frontend install
pnpm --dir app/frontend dev
```

For a single-process build (backend serves the built SPA):

```bash
pnpm --dir app/frontend build
uv run uvicorn app.backend.app:app --port 8787   # open http://localhost:8787
```

## Architecture

```
backend/   FastAPI API over the shared SQL store (data.py → service.py → app.py)
           safety_guard.py — subprocess and bot-write allowlists (load-bearing)
frontend/  Vite + React + TypeScript SPA
agent/     Narrow helper commands and operating context for the Ask pane
cache/     Per-machine session handles and regenerable diff data (gitignored)
```

The SQL store is the source of truth for pipeline, decision, chat-memory, and
activity data. `data.py` maintains an incrementally refreshed in-memory
projection, `service.py` shapes records for the SPA, and `app.py` exposes the
HTTP and SSE API. Local cache files are disposable; they are not a second store.
See the repository-level `ARCHITECTURE.md` for the complete data model and
`agent/context.md` for the Ask pane's command boundary.
