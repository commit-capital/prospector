# store-read bulk reads and the Explorer filter in chat

## Problem

The app chat agent reads the pipeline store through one allowlisted helper,
`prospector_app/agent/store-read`, whose subcommands each return one record
(`pr N`, `cluster CID`, `issue N`) or one registry. A question over the whole
population ("which open PRs are linked to issues that are now closed?") has no
sanctioned path: the agent's shell is a network-disabled sandbox, so it cannot
open the shared Postgres store itself, and the Explorer context injected into
its prompt details at most 150 of the operator's filtered PRs and tells the
agent to ask for a narrower filter.

## Design

### 1. Bulk list subcommands on `store-read`

Two new subcommands print a JSON array of compact rows, one per record, sorted
by number, read through `Store.all_prs()` and `IssueStore.all_issues()`:

    store-read prs    [--state open|closed|all] [--numbers N,N,...] [--fields path,path,...]
    store-read issues [--state open|closed|all] [--numbers N,N,...] [--fields path,path,...]

- `--state` defaults to `open` on both. Every stored record is kept, so a
  closed issue or a closed PR is one flag away.
- `--numbers` restricts the output to the listed numbers (a comma-separated
  list), which is how the agent scopes a read to the operator's current
  Explorer set.
- `--fields` names dotted paths into the raw record (`issues.linked`,
  `analysis.disposition`, `meta.updated_at`). Each is copied into the row at
  its nested position; a path the record does not hold reads `null` at the
  leaf. The default row is deliberately small so a 3,000-row read stays cheap
  to pipe:
  - PR: `pr, state, title, author, head_sha, updated_at`
  - issue: `issue, state, state_reason, title, author, updated_at`

- `prs --with-issues` hydrates every `issues.linked` entry with the linked
  issue's current `state`, `state_reason`, and `title`, read in one query
  through `IssueStore.load_issues`. The PR-side link is the authoritative one
  (ingest refreshes it from live PR bodies, while an issue's candidate
  snapshot only refreshes when the issue changes), and the agent's shell has no
  writable scratch space or command substitution to join two reads with, so
  the one cross-collection edge a PR record holds is joined by the helper.

Filters compose with `jq`, which the agent's allowlist already admits. The agent manual (`prospector_app/agent/context.md`) documents the
commands and one worked join, and tells the agent to pipe bulk output through
`jq` rather than print it into its context.

No SQL surface is added; the helper stays on the validated store accessors.

### 2. The chat request carries the Explorer's filter spec

`GET /api/chat` accepts `spec`, the Explorer's current filter spec as JSON
(the same spec the Explorer sends to the PR list endpoint, including a deep
search overlay's `numbers`). When present, the backend evaluates it in-process
with `service.query_prs(spec, limit=0)` and grounds the session on the result:
the spec itself, a detail line for each of the first 150 matches (as today),
and, when the match exceeds that cap, the full list of matching PR numbers with
the `store-read prs --numbers` recipe that reads them. The "ask the operator to
narrow the filter" line goes away. `prs` and `prs_total` keep their meaning for
callers that send numbers only.

The frontend replaces the capped `prs` list with the spec: the Explorer
publishes `{ids, spec}` to the agent pane, which sends `spec`.

## Testing

- `test_store_read_cli.py`: `prs`/`issues` default to open records, `--state`
  widens, `--numbers` restricts, `--fields` projects nested paths and reads
  `null` for a missing section, `--with-issues` hydrates linked issues and
  leaves an unknown issue's fields `null`.
- `test_chat_context.py`: a spec grounds the session from the server-side
  match, and an over-cap match lists every number without the narrow-the-filter
  line.
- Frontend: `pnpm run build` and lint on the touched files.
