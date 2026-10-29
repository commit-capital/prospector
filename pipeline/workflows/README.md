# Agentic phases — how the workflows run

Python runners own summaries, incremental assignment, analysis, and security
review. They run locked-down headless agents and validate and commit completed
results as they arrive. After interruption, rerun the command: current results
are skipped. Full reclustering, straddle assignment, and Greptile classification
use the three JavaScript workflows here, with explicit driver commit steps.

## Phase 1 — CLUSTER

```
# stage A: summaries (only PRs lacking a current summary)
uv run python pipeline/cluster_pass.py --summarize-only --limit 200

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
uv run python pipeline/cluster_pass.py --limit 200 --analyze 20
uv run python pipeline/views.py
```

The pass refreshes summaries and places new PRs into existing clusters, new
ones, or standalone. A join reopens only that cluster for ANALYZE. It also
detaches stale memberships once their summaries are current; another pass
re-homes a PR whose summary was stale at the start. Control `cluster-new` and
the `TRIAGE_CLUSTER_WORKER` lane use this same runner.

To let already-clustered PRs join additional clusters (a PR may belong to
several), run `write-straddle-units` → `straddle.js` → `commit-assign-dir`. It
only adds memberships.

## Phase 2 — ANALYZE

```bash
uv run python pipeline/analyze_clusters.py --limit 200 --concurrency 4
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

Estimate before launching a wave. The optional usage log is a prior Claude
Workflow directory with `agent-*.jsonl` transcripts, and
`--require-calibration` refuses to fall back to heuristic constants:

```
uv run python pipeline/security_driver.py estimate --max 25 \
  --usage-log <PAST_WORKFLOW_LOG_DIR> --require-calibration \
  --usage-percentile p90 --verify-chunk-size 4 --max-metered-tokens <LIMIT>
uv run prospector security-review --pending --limit 25
```

The wave rechecks eligibility before each PR, then uses the same review engine
as `uv run prospector security-review --pr N` and the verify hunter. Each PR's
three lenses run concurrently; findings are refuted in chunks. Service outages,
overload, and capacity stops end the wave without discarding committed reviews.
Incomplete reviews remain eligible on the next run. The estimator's
`--verify-chunk-size` changes its projection only; the engine's chunk size is
owned by `security_driver.VERIFY_CHUNK_SIZE`.

## Phase 6 — VERIFY

Prove the claimed bug on a pinned default branch, apply the fix, and prove it
resolves. Needs Docker; `sandbox/README.md` has the machine checklist. Build
the image once, then pin a base per default-branch revision:

```
uv run python pipeline/verify_driver.py build-image
uv run python pipeline/verify_driver.py prepare-base --tier 1
```

Verification does not run the repository's full suite: upstream CI runs it on
every PR, and the merge gate requires CI green at the current head. The operator queues a PR from the app,
and the `TRIAGE_VERIFY_WORKER` machine runs `pipeline/verify_pr.py` for one PR
at a time:

- the blind adequacy verdict is committed before any sandbox boots;
- apply-check → [repro] → red → green run in separate containers, then the
  confirm re-run, the compile/build lanes and the e2e leg on a clean red→green;
- the judge's ratings and the host-observed exit codes go to
  `gates.verify_outcome`, which computes the outcome.

The agent never picks the outcome, and a red counts only on the sandbox's
sentinel exit code. A run that errors is held with no verdict; re-queue it.
