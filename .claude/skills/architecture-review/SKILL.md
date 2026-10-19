---
name: architecture-review
description: Use before opening any Prospector PR, and before writing code for a change to the store, a worker sweep, a cache or an upstream write path. Checks the design for data in the wrong place, costs that grow with history, and docs in the wrong file.
---

# Architecture review

## Before coding: sign-off

Stop and get the operator's OK before writing code that:

- bumps `schema.STORE_SCHEMA_VERSION`, or adds a store table, record section or
  registry key;
- adds a worker loop or sweep;
- adds a cache;
- adds an upstream write path.

Say it in four or five sentences in chat, not a doc: where the data lives, what
one read costs at live scale, how it is written, and how both grow with
history. Anything else needs no sign-off.

## Before the PR: review the diff

Have a fresh subagent review `git diff origin/main...HEAD` against the warning
signs below. Each one it finds is a redesign, not a note in the PR.

- **Per-item data in a shared value.** State about one PR or issue lives on
  that record as a section (`storekit.Collection.set_section`). A `registries`
  row is for data that has no single owning record, like the threat blocklist.
- **A cache that makes a read affordable.** If a read needs a cache to be
  affordable, the data is in the wrong place or computed at the wrong time.
  Write the fact where the reader finds it.
- **Whole-store work per request or per row.** A list endpoint that reads every
  PR, issue or the activity log for each row, or each request.
- **Cost that grows with history.** A recurring sweep should read what changed
  since its last pass. A pass that re-reads everything ever acted on grows
  every day.
- **Recomputing a fact on read when it was already in hand.** "Store facts, not
  conclusions" means compute cheap derivations on read. If a sweep already
  observed the fact, store it where the sweep saw it.
- **Skipping a schema bump to keep the diff small.** Take the bump.
- **Feature prose in `AGENTS.md`.** It holds invariants only. Feature docs go in
  the module docstring, `docs/`, or the PR description.

## Measure

When the change touches a read path or a sweep, time it against the configured
store (`TRIAGE_STORE_URL`), not test fixtures, and put the before and after
numbers in the PR description.

## Scope

One concern per PR. If the diff spans unrelated backend, frontend and store
changes, split it so a reviewer can see the design.
