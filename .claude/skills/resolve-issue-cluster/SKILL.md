---
name: resolve-issue-cluster
description: Execute a curated issue cluster's close-as-dup actions upstream via the app executor as the configured bot, gated on confirmed curation. Close-as-dup is implemented; request-repro / link-pr remain surfaced suggestions.
---

# resolve-issue-cluster

The issue-side resolve step, mirroring the app's PR resolution. Resolve
`TRIAGE_REPO` and `TRIAGE_BOT_LOGIN` from the process environment or the
gitignored root `.env` before starting; stop if either is missing. The one
upstream write — **close-as-dup** — is implemented and runs through the app
executor as the configured GitHub App, gated and logged like every other
upstream write.

## Where execution happens

The app's **Issues tab** is the surface. The close-as-dup worklist
(`/api/issues/duplicates`) lists the confirmed duplicate groups, most painful
first; closing one calls `POST /api/execute/issue/<n>/close-dup`, which runs
`executor.close_issue` — post a comment pointing at the canonical, then close
the issue `duplicate` of it, as `TRIAGE_BOT_LOGIN`. The action is reversible
(reopen + delete the comment) and appended to the activity log.

## The gate

A close is allowed only when `issue_gates.close_dup_eligibility` passes (via
`issues.close_dup_gate`): the issue is routed `close-dup` with a canonical, its
cluster's `curation.confirmed` is true (set by `/diagnose-issue-cluster`), the
cluster is not `needs_review`, the analysis is fresh, and the canonical is still
open upstream. An unconfirmed or stale cluster is refused — even in dry-run. On
a machine where the configured bot token cannot be minted, every write is
forced to dry-run.

## Prerequisites

1. The issue store is populated (`issue_triage/issue_pipeline.py`).
2. The cluster has been curated and **confirmed** by `/diagnose-issue-cluster`
   (its `curation.confirmed` is true and a canonical is set).
3. The member issues are routed `close-dup` (the ANALYZE phase, or the curation
   import) with that canonical.

## Scope

- **Implemented:** close-as-dup (the gated executor path above).
- **Not executed (surfaced suggestions only):** `request-repro` and `link-pr`
  dispositions are shown on the issue rows but are not gated upstream writes; act
  on them manually if desired. Merge has no analog (issues aren't merged).
