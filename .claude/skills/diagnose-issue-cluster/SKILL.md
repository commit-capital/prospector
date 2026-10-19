---
name: diagnose-issue-cluster
description: Use when given an issue cluster ID and asked to confirm/split the cluster, pick a canonical issue, identify duplicates, grade reproduction quality, and link candidate PRs. Read-only upstream; writes a local briefing and the cluster's curation in the store.
---

# diagnose-issue-cluster

Given an issue cluster ID (`/diagnose-issue-cluster 3`, or `C003` /
`cluster-003`), curate the cluster, pick its canonical issue, name the
duplicates, note reproduction quality, and list candidate fixing PRs. The
skill is read-only upstream. It writes a local briefing and the cluster's
`curation` in the store, which the close-as-dup gate requires.

The store must be populated (`uv run python issue_triage/issue_pipeline.py`,
`--skip-fetch` to reuse it). Read it through the typed store, never raw rows:

```python
from issue_triage.issue_store import IssueStore
store = IssueStore()
cl = store.load_issue_cluster(cid)  # subsystem, members, pain, needs_review, curation
iss = store.load_issue(n)           # title, body, author, comments, reactions_total, repro_grade, candidate_prs
```

## Steps

1. **Curate**: confirm that every member describes the same problem, propose a
   split when distinct mechanisms share an identifier, or list members that
   belong elsewhere under `reclassify`.
2. **Canonical and duplicates**: pick the issue with the clearest
   reproduction, then the earliest and most engaged. Give each duplicate one
   line of evidence. Duplicates' engagement counts toward the canonical's
   pain, so never drop one.
3. **Reproduction**: say what the canonical lacks (steps, expected vs actual,
   environment, trace). A D or F canonical is a `request-repro` candidate.
4. **Candidate PRs**: list explicit `Fixes #N` links and subsystem leads from
   `iss.candidate_prs`. High pain with no fixing PR is a priority gap; say so.
5. **Write** the briefing to `docs/issue-briefings/cluster-<NNN>.md`, then
   record the verdict:

   ```python
   cl = store.edit_issue_cluster(cid)
   cl.record_curation({
       "confirmed": True,  # the close-as-dup gate requires this
       "canonical": canonical_n,
       "label": "<one-line topic>",
       "duplicates": [{"issue": n, "evidence": "..."}],
       "reclassify": [{"issue": n, "target": other_cid, "reason": "..."}],
       "repro_notes": "...",
       "priority_gap": False,
   })
   ```

   For a split, create each sub-cluster with `store.create_issue_cluster`,
   `set_members`, and its own confirmed curation, then narrow the original.
   An unconfirmed cluster stays out of the close worklist.

Closing duplicates upstream is `/resolve-issue-cluster`.
