---
name: diagnose-issue-cluster
description: Use when given an issue cluster ID and asked to confirm/split the cluster, pick a canonical issue, identify duplicates, grade reproduction quality, and link candidate PRs. Read-only; writes a local briefing + verdict JSON.
---

# diagnose-issue-cluster

Reads a deterministic issue cluster, curates it (confirm / split / merge), selects a **canonical** issue, identifies duplicates **with their engagement rolled into the canonical's pain score** (duplicates are signal, not noise), notes reproduction quality, and lists candidate fixing PRs. Produces a dev-facing briefing.

This is the issue-side mirror of the pipeline's PR-cluster analysis. Resolve
`TRIAGE_REPO` and `TRIAGE_BOT_LOGIN` from the process environment or the
gitignored root `.env` before starting; stop if either is missing. Read upstream
state through `gh`. The skill is **read-only against `TRIAGE_REPO`** — it never
comments, closes, labels, or edits an upstream issue. It writes a local briefing
and the cluster's `curation` section in the issue store, which the close-as-dup
gate consumes (a close is only allowed once curation is confirmed).

## Prerequisites

The deterministic pipeline must have run at least once so the store is populated:

```bash
python issue_triage/issue_pipeline.py              # full (live read-only fetch)
python issue_triage/issue_pipeline.py --skip-fetch # reuse the store
```

All inputs come from the issue store (`issue_triage/store/`), read via `IssueStore`
(never hand-read the JSON):
- `store.load_issue_cluster(cid)` — membership, subsystem, pain, `needs_review`, existing curation
- `store.load_issue(n)` — each member's body + engagement (`meta`), `repro` grade, `links` (candidate PRs)

## Input

Cluster ID as a number or `C0NN` / `cluster-NNN` form. Example: `/diagnose-issue-cluster 1` or `/diagnose-issue-cluster cluster-003`.

## Steps

### 1. Load the cluster

Normalize input to the integer cluster id. `cl = store.load_issue_cluster(cid)` gives `subsystem`, `members`, `pain`, `needs_review`, and any prior `curation`. For each member `n`, `iss = store.load_issue(n)` gives `iss.title` / `iss.body` / engagement (`iss.reactions_total`, `iss.comments`, `iss.author`), `iss.repro_grade`, and `iss.candidate_prs`.

### 2. Curate the cluster (confirm / split / merge)

Read each member's title + body. Decide:
- **Confirm:** all members really describe the same underlying problem.
- **Split:** the deterministic pass over-merged (distinct root causes share an identifier) → propose sub-clusters, each with its own canonical.
- **Merge note:** members clearly belong to another cluster → list them under `reclassify` with the target cluster + reason.

Distinguish *mechanisms*, don't lump.

### 3. Select the canonical + enumerate duplicates

The canonical is the issue that best represents the problem: clearest reproduction (highest repro grade), earliest/most-engaged, least bundled noise. For every other member, write one line of duplicate evidence (why it's the same problem) and confirm its engagement is already summed into the canonical's pain. **Never discard a duplicate's signal** — the dupe count and rolled-up reactions/comments are the data that justify the pain rank.

### 4. Note reproduction quality

For the canonical and notable members, summarize the repro grade and what's missing (no steps / no expected-vs-actual / no env / no trace). Flag canonicals graded D/F as `request-repro` candidates.

### 5. Link candidate PRs

From the canonical issue's `iss.candidate_prs`, list explicit (`Fixes #N`) links and subsystem-match leads. If the canonical is a **priority gap** (high pain, no explicit fixing PR), say so plainly — this is a signal to write or prioritize a fix. If an existing PR (esp. one in `STATUS.md`'s Wave plan) maps to a top pain cluster, recommend promoting it.

### 6. Write the briefing + curation

Write the human briefing to `docs/issue-briefings/cluster-<NNN>.md` (root cause, canonical, duplicates with evidence, repro notes, candidate PRs, suggested actions per member).

Then record the verdict into the store via the typed model — never hand-edit JSON:

```python
cl = store.edit_issue_cluster(cid)
cl.record_curation({
    "confirmed": True,            # the close-as-dup gate requires this
    "canonical": <canonical_n>,
    "label": "<one-line topic>",
    "duplicates": [{"issue": n, "evidence": "..."}, ...],
    "reclassify": [{"issue": n, "target": <cid>, "reason": "..."}, ...],
    "repro_notes": "...",
    "priority_gap": <bool>,
})
```

`record_curation` mirrors the named `canonical` onto the cluster. For a **split**, create the sub-clusters with `store.create_issue_cluster(...)`, `set_members([...])`, and a confirmed `record_curation` each, then narrow the original. Leaving `confirmed` off (or false) keeps the cluster out of the close worklist.

The briefing is a local file; the curation write stays in the **local store** —
this skill runs no GitHub write verb.

## Read-only contract

This skill performs reads via the operator's `gh` login (or the store) and
writes only to the local repo + local store. The sanctioned upstream write path
is `/resolve-issue-cluster` (the gated app executor as
`TRIAGE_BOT_LOGIN`) — see that skill.
