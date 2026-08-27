# Agentic phases — how the workflows run

Drivers own wave selection, validation, store writes, and stable IDs. CLUSTER,
ANALYZE, and SECURITY use schema-validated Claude Workflows; VERIFY uses
locked-down per-PR agents. Agent output always returns through a driver.

## Phase 1 — CLUSTER (two stages)

```
# stage A: summaries (re-runnable; only PRs lacking a current summary)
uv run python pipeline/cluster_driver.py fetch-diffs --max 200
uv run python pipeline/cluster_driver.py write-batches --max 200   # pre-splits the wave + resets /tmp/pipeline-out
#   → run the summarize-pr-diffs workflow (reads /tmp/pipeline-batches/index.json)
#     each batch agent writes its slice to /tmp/pipeline-out/ as it finishes
uv run python pipeline/cluster_driver.py commit-summaries-dir       # commits every batch file on disk
# Durable at batch granularity: if the run dies mid-pass, re-running commit-summaries-dir
# lands the finished batches, then re-run the wave for the rest (committed PRs are skipped).

# stage B: clusters (over ALL current summaries, grouped by subsystem)
# (fresh slate first: `reset-clusters` drops old clusters + PR backrefs before re-clustering)
# RE-INGEST + RE-SUMMARIZE FIRST: a full recluster clusters the store snapshot, which is
# only as fresh as the last INGEST. write-cluster-units prints a `stale-input warning` (and
# returns it as `stale_input_warning`) when the last INGEST is more than 12h old — heed it
# and re-ingest, or you'll group heads the author has since moved (issue #253).
uv run python pipeline/cluster_driver.py write-cluster-units   # [--chunk N]: large N = one unit per subsystem
#   → run the propose-clusters workflow (reads /tmp/pipeline-cluster-units/index.json)
#     each unit agent writes its proposals to /tmp/pipeline-cluster-out/ as it finishes
uv run python pipeline/cluster_driver.py commit-clusters-dir   # commits every unit's proposals on disk
uv run python pipeline/views.py
# Durable per unit. Don't commit a PARTIAL pass — mark_standalone would wrongly stamp the
# un-processed subsystems' PRs as standalone; resume the missing units first.
```

`commit-clusters-dir` finishes by stamping every considered-but-unplaced PR
standalone (`mark_standalone`), so a full pass leaves the store consistent — no
separate step.

### Incremental: assigning new PRs without re-clustering

A from-scratch re-cluster re-runs the granularity lottery over the whole corpus
and churns ~23% of the existing (already analyzed/reviewed) clusters. To fold in
PRs that arrived *after* a clustering pass — without disturbing the reviewed
clusters — use the assignment mode:

```
uv run python pipeline/cluster_driver.py reset-stale-memberships  # detach PRs whose head moved since clustering
uv run python pipeline/cluster_driver.py write-assign-units   # one unit per subsystem with new PRs
#   → run the assign-new-prs-to-clusters workflow (reads /tmp/pipeline-assign-units/index.json)
#     each agent sees its subsystem's existing clusters as FROZEN anchors and only
#     decides, per new PR: join an existing cluster / form a new one / standalone
uv run python pipeline/cluster_driver.py commit-assign-dir    # applies every unit's assignment on disk
uv run python pipeline/views.py
```

Existing clusters are **append-only** here — a join reopens just that cluster
(`outcome=None`) so ANALYZE re-runs on it alone; clusters that gain nothing are
untouched. "New PRs" are the never-clustered ones (no `cluster` section);
already-standalone PRs are left as-is. Durable per unit, same as the full pass.

`reset-stale-memberships` runs first because membership is otherwise sticky across
head moves: a PR force-pushed to unrelated content after it was clustered stays in
a cluster whose root problem its new diff no longer addresses. It resets **only**
the PRs whose head actually moved (clearing their `cluster` section so the assign
pass that follows re-homes them on their current diff) — not a full re-cluster, so
reviewed clusters that kept their members are untouched. Stale *standalone* stamps
are cleared the same way, returning those PRs to the assign pass's candidate pool.
A cluster emptied by a detach is dropped. Run SUMMARIZE first so the re-homing
sees a current summary.

### Backfill: straddlers (a PR that belongs to more than one cluster, #196)

The assign pass above already lets a NEW PR straddle (a secondary-concern join).
To backfill the EXISTING clustered corpus — additively, without re-partitioning —
run the straddle pass:

```
uv run python pipeline/cluster_driver.py write-straddle-units   # one+ unit per subsystem of clustered PRs
#   → run the straddle-clustered-prs workflow (reads /tmp/pipeline-straddle-units/index.json)
#     each agent proposes, per PR, ADDITIONAL existing clusters a secondary concern advances
uv run python pipeline/cluster_driver.py commit-assign-dir      # same additive commit path as assign
uv run python pipeline/views.py
```

A gained membership reopens just that cluster (`outcome=None`) so ANALYZE re-runs
on it alone (Phase 2). Purely additive — a straddle pass never removes a PR from
a cluster it already belongs to.

Waves are idempotent: summaries are stamped `against_head_sha`, so re-running a
wave only touches PRs whose head moved or that were never summarized. Cluster
IDs are stable across re-runs (member-overlap matching in the driver).

Singletons are NOT clusters: a PR with no ≥2-member group stays unclustered, but
a pass stamps it standalone — a `cluster` section with no `id`, freshly stamped
against the head. That distinguishes "considered, left standalone" from "no pass
has reached it yet" via `is_current(rec, "cluster")`, and the app card
reflects the difference. Standalone PRs are still handled through the PR Queue /
Easy / Stale lanes.

## Phase 2 — ANALYZE

```bash
uv run python pipeline/analyze_driver.py write-bundles --max 200
# run analyze.js; it writes one result per cluster to /tmp/pipeline-analyze-out
uv run python pipeline/analyze_driver.py commit-dir
uv run python pipeline/views.py
```

The driver selects stale or unanalyzed clusters, validates every disposition,
and reconciles a PR that belongs to several clusters to its most-blocking
proposal. Completed cluster files are durable and can be committed after an
interrupted Workflow run.

## Phase 0.5 — THREAT SCAN (deterministic, no agents)

Runs after INGEST and after diffs are fetched (the CLUSTER `fetch-diffs` step).
Cheap and idempotent — no Workflow, no metered tokens.

```
uv run python pipeline/threat_scan.py            # scan every open PR with a cached diff
uv run python pipeline/threat_scan.py --only 5174,5270   # rescan specific PRs
uv run python pipeline/views.py
```

It scans each PR's cached diff against the signatures in `threats.py` (the ONE
threat policy: obfuscated self-decoders, capability smuggles, build-config
require-injection, EOL-churn camouflage) and checks the author against the
durable actor blocklist in the store's `threats` registry. A `malicious` verdict stamps the
PR's `threat` section and, on first detection, blocks the author and logs the
incident. `gates.pr_clean` then refuses the PR forever (fail-closed, no
staleness exemption), so a flagged PR can never reach security review or merge —
even if Greptile scores it 5/5 and CI is green. A blocked author's *future* PRs
are flagged on sight, before any diff is fetched. Repository maintainers (the
profile's `trusted_authors`) are never flagged: their PRs always stamp `clear`,
though a leaked credential still raises a rotate-secret action item.

### GREPTILE READ

Reads the Greptile entry the ingest stored for each below-bar PR (its summary
comment and inline findings) and classifies every finding substantive-vs-nitpick.

```
uv run python pipeline/greptile_read_driver.py write-batches
# run the workflow: greptile_read.js  (writes /tmp/pipeline-greptile-out)
uv run python pipeline/greptile_read_driver.py commit-dir
```

## Phase 4 — SECURITY

Always estimate before launching the Fable Workflow:

```
uv run python pipeline/security_driver.py usage <PAST_WORKFLOW_LOG_DIR>
uv run python pipeline/security_driver.py estimate --max 25 \
  --usage-log <PAST_WORKFLOW_LOG_DIR> \
  --require-calibration \
  --usage-percentile p90 \
  --verify-chunk-size 4 \
  --max-metered-tokens <TOKEN_LIMIT>
uv run python pipeline/security_driver.py eligible --max 25 > /tmp/security-wave.json   # resets /tmp/security-out
#   → run the pr-security-review workflow (reads /tmp/security-wave.json)
#     each PR's verdict is written to /tmp/security-out/ the moment it's computed
uv run python pipeline/security_driver.py commit-dir   # commits every verdict on disk
# Durable per PR: kill the run anytime, commit-dir lands the finished PRs, then
# re-run eligible (only PRs still lacking a current verdict re-queue).
```

Use a prior Claude Workflow directory with `agent-*.jsonl` transcripts as the
usage log. `estimate --usage-log` derives findings-per-PR and p50/p90/p95/max
per-agent usage from real review/verifier runs; `--require-calibration` fails
closed instead of silently falling back to heuristic constants.

The security Workflow keeps Fable on both review and verification, but verifier
fan-out is chunked, not per finding. A PR with 12 non-green findings gets three
focused verifier agents at `--verify-chunk-size 4`, instead of 12 verifier
agents that each reread the same diff. Review/verifier prompts also tell agents
to use cached diffs first and only fetch live upstream source when a concrete
finding needs more context.

## Phase 6 — VERIFY

Dynamic verification: prove the claimed bug is real on a pinned `main`, apply the
fix, and prove it resolves. Runs on GATE-clean merge candidates (the same
GATE-clean rule as SECURITY) and adds `verified-fix` as a fifth merge-bar element.

Needs Docker (`colima start` if it is down). A fresh machine first builds the
hardened image (`uv run python pipeline/verify_driver.py build-image` — bakes the
profile's pnpm pin); sandbox/README.md carries the full machine checklist.

Pin and prepare the base image once per default-branch revision:

```
uv run python pipeline/verify_driver.py prepare-base --tier 1
#   pins the default branch + tier, clones it, asserts no secrets, and builds
#   pr-verify-base:<sha12>-t1, then runs the full suite ONCE against that
#   pristine tree (the baseline phase, ~6-15+ min — measured on the first run,
#   see sandbox/README.md's measurement checkpoint) and refuses to pin without
#   a captured baseline: no pin means no exclusion set, so the regress phase
#   cannot tell a pre-existing failure from a regression. A profile without a
#   verify.suite contract pins without a baseline run and the regress leg is
#   skipped for that repository.
uv run python pipeline/views.py
```

With a base pinned, verification runs per-PR from the app: the operator
clicks **Queue for verification** on a PR, and the verification worker
(`TRIAGE_VERIFY_WORKER=1`, the machine with the sandbox) drains the queue by
running `pipeline/verify_pr.py` for one PR at a time. That runner:

- commits Signal 1 (blind adequacy) to the store BEFORE any sandbox boots, so it
  cannot be rationalized backward from a green result;
- runs apply-check → [repro] → red → green, each its own container, reading each
  result from the exit code; the tier comes from the pin, so it always boots the
  image prepare-base built. On a clean red→green only, it also runs the regress
  phase against the pinned baseline's exclusion set, plus one confirming
  fresh-container re-run when that first regress run fails;
- judges the run (Signal 3, and Signal 4 when a repro ran) and lets
  `gates.verify_outcome` compute the outcome.

The blind and judge agents run as locked-down headless `claude -p` subprocesses;
their evidence stays in memory, so nothing is staged on disk. A run that errors
is HELD with no verdict — re-queue the PR to run it again.

**The agent never picks the outcome.** The judge rates whether the red failed for
the claimed reason, and — when an independent repro ran — whether it did too;
`gates.verify_outcome` computes the outcome from the four signals (the repro's
reason-match is recorded but does not itself change the outcome — Signal 4 stays
corroborating evidence, not a gate). The escalation rule — blind says the test is
unfaithful, yet it goes clean red→green → `needs-human` — is therefore
unreachable from a prompt. That escalation is the highest-value output of the
phase.

**The host owns the verdict.** Each phase is its own `docker run`; a red is
accepted only on the exact sentinel exit code 20. Anything the test prints is
advisory evidence for the judge, never a verdict.
