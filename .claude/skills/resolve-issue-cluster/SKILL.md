---
name: resolve-issue-cluster
description: Execute a curated issue cluster's close-as-dup actions upstream via the app executor as the configured bot, gated on confirmed curation. Close-as-dup is implemented; request-repro / link-pr remain surfaced suggestions.
---

# resolve-issue-cluster

Close a curated cluster's duplicates upstream. The only write is close-as-dup,
and it runs through the app executor as `TRIAGE_BOT_LOGIN`, gated and logged.

**Where**: the app's Issues tab lists confirmed duplicate groups
(`/api/issues/duplicates`). Closing one calls
`POST /api/execute/issue/<n>/close-dup` → `executor.close_issue`, which comments
with the canonical and closes the issue as a duplicate. Reopening reverses it.

**Gate**: `issue_gates.close_dup_eligibility` (through
`issues.close_dup_gate`) owns the conditions; the main one is a cluster
confirmed by `/diagnose-issue-cluster`. A refused issue is refused in dry-run
too, and a machine that cannot mint the bot token only dry-runs.

`request-repro` and `link-pr` are shown on issue rows but not executed; act on
them by hand.
