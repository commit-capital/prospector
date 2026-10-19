# Agentic phases — how the workflows run

Drivers own wave selection, validation, store writes, and stable IDs. CLUSTER,
ANALYZE, and SECURITY use schema-validated Claude Workflows (`*.js` here);
VERIFY uses locked-down per-PR agents. Agent output always returns through a
driver. Every wave is durable: each agent writes its result to disk as it
finishes, so after an interrupted run the driver's commit step lands the
finished work and a re-run picks up only what is missing.

## Phase 1 — CLUSTER

```
# stage A: summaries (only PRs lacking a current summary)
uv run python pipeline/cluster_driver.py fetch-diffs --max 200
uv run python pipeline/cluster_driver.py write-batches --max 200
#   → run summarize.js (reads /tmp/pipeline-batches/index.json)
uv run python pipeline/cluster_driver.py commit-summaries-dir

# stage B: clusters over all current summaries, grouped by subsystem
# Re-ingest first: heed write-cluster-units' stale-input warning.
# `reset-clusters` gives a fresh slate.
uv run python pipeline/cluster_driver.py write-cluster-units   # [--chunk N]
#   → run cluster.js (reads /tmp/pipeline-cluster-units/index.json)
uv run python pipeline/cluster_driver.py commit-clusters-dir
uv run python pipeline/views.py
```

`commit-clusters-dir` stamps every considered-but-unplaced PR standalone, so
never commit a partial stage B: resume the missing units first. A standalone PR
has a current `cluster` section with no `id`, which distinguishes "considered"
from "not reached yet".

### Incremental: new PRs without re-clustering

A full re-cluster churns existing, already-reviewed clusters. To fold in PRs
that arrived since, treat existing clusters as frozen anchors:

```
uv run python pipeline/cluster_driver.py reset-stale-memberships  # detach PRs whose head moved
uv run python pipeline/cluster_driver.py write-assign-units
#   → run assign.js (reads /tmp/pipeline-assign-units/index.json)
uv run python pipeline/cluster_driver.py commit-assign-dir
uv run python pipeline/views.py
```

Each new PR joins a cluster, forms a new one, or stays standalone. A join
reopens only that cluster for ANALYZE. Run summaries first so a detached PR is
re-homed on its current diff. The same pass runs headless as
`uv run python pipeline/cluster_pass.py [--limit N] [--analyze N]` (Control
`cluster-new`, and the `TRIAGE_CLUSTER_WORKER` lane).

To let already-clustered PRs join additional clusters (a PR may belong to
several), run `write-straddle-units` → `straddle.js` → `commit-assign-dir`. It
only adds memberships.

## Phase 2 — ANALYZE

```bash
uv run python pipeline/analyze_driver.py write-bundles --max 200
#   → run analyze.js (writes /tmp/pipeline-analyze-out)
uv run python pipeline/analyze_driver.py commit-dir
uv run python pipeline/views.py
```

The driver picks stale or unanalyzed clusters and validates every disposition.
A PR in several clusters takes the most blocking proposal.

## THREAT SCAN (deterministic, no agents)

Runs after diffs are fetched; worker machines also scan new heads on their own
(`prospector_app/backend/threat_refresh.py`, `pr_watch.py`). The signatures and
blocklist live in `threats.py`; the scan's rules are in `threat_scan.py`'s
docstring.

```
uv run python pipeline/threat_scan.py                   # every open PR
uv run python pipeline/threat_scan.py --only 5174,5270  # specific PRs
uv run python -m pipeline.threat_evidence capture --backfill
uv run python -m pipeline.threat_evidence export --pr <n> --out <dir>
uv run python -m pipeline.threat_evidence verify        # re-hash stored artifacts
```

A `malicious` verdict blocks the PR at every head and blocks its author.
`threat_evidence` keeps an append-only, read-only capture of each malicious head.

## GREPTILE READ

Classifies each below-bar PR's stored Greptile findings as substantive or nitpick.

```
uv run python pipeline/greptile_read_driver.py write-batches
#   → run greptile_read.js (writes /tmp/pipeline-greptile-out)
uv run python pipeline/greptile_read_driver.py commit-dir
```

## Phase 4 — SECURITY

Estimate before launching the workflow. The usage log is a prior Claude
Workflow directory with `agent-*.jsonl` transcripts, and
`--require-calibration` refuses to fall back to heuristic constants:

```
uv run python pipeline/security_driver.py estimate --max 25 \
  --usage-log <PAST_WORKFLOW_LOG_DIR> --require-calibration \
  --usage-percentile p90 --verify-chunk-size 4 --max-metered-tokens <LIMIT>
uv run python pipeline/security_driver.py eligible --max 25 > /tmp/security-wave.json
#   → run security.js (reads /tmp/security-wave.json)
uv run python pipeline/security_driver.py commit-dir
```

Verifiers check findings in chunks of `--verify-chunk-size`, not one agent per
finding. Unattended review runs through `uv run prospector security-review`.

## Phase 6 — VERIFY

Prove the claimed bug on a pinned default branch, apply the fix, and prove it
resolves. Needs Docker; `sandbox/README.md` has the machine checklist. Build
the image once, then pin a base per default-branch revision:

```
uv run python pipeline/verify_driver.py build-image
uv run python pipeline/verify_driver.py prepare-base --tier 1
```

`prepare-base` also runs the suite once on the pristine tree, so a regression
can be told from a pre-existing failure. The operator queues a PR from the app,
and the `TRIAGE_VERIFY_WORKER` machine runs `pipeline/verify_pr.py` for one PR
at a time:

- the blind adequacy verdict is committed before any sandbox boots;
- apply-check → [repro] → red → green run in separate containers, then the
  regress phase on a clean red→green;
- the judge's ratings and the host-observed exit codes go to
  `gates.verify_outcome`, which computes the outcome.

The agent never picks the outcome, and a red counts only on the sandbox's
sentinel exit code. A run that errors is held with no verdict; re-queue it.
