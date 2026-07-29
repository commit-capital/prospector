export const meta = {
  name: 'assign-new-prs-to-clusters',
  description: 'Incremental: place new PRs into existing clusters, new ones, or standalone',
  phases: [
    { title: 'Index', detail: 'read the unit index' },
    { title: 'Assign', detail: 'one agent per subsystem with new PRs' },
  ],
}
// The driver (`cluster_driver.py write-assign-units`) writes
// /tmp/pipeline-assign-units/unit-NNN.json + index.json. Each unit is
// {subsystem, existing_clusters, new_prs}. Existing clusters are FROZEN anchors —
// the agent only decides where each never-clustered PR goes, so the analyzed /
// reviewed clusters are never re-partitioned (a full re-cluster churns ~23% of
// them; this touches only the clusters that gain a member).
const INDEX_PATH = '/tmp/pipeline-assign-units/index.json'
// Where each agent persists its unit's assignment; cluster_driver.py owns this dir
// (ASSIGN_OUT_DIR) and commits it via `commit-assign-dir`.
const OUT_DIR = '/tmp/pipeline-assign-out'

const INDEX_SCHEMA = { type: 'object', properties: {
  count: { type: 'integer' }, units: { type: 'array', items: { type: 'string' } },
  repo: { type: 'string', description: 'owner/name of the repository the units came from' } },
  required: ['count', 'units', 'repo'] }

const ASSIGN_SCHEMA = { type: 'object', properties: {
  joins: { type: 'array', items: { type: 'object', properties: {
    pr: { type: 'integer' }, cluster_id: { type: 'integer' } },
    required: ['pr', 'cluster_id'] } },
  new_clusters: { type: 'array', items: { type: 'object', properties: {
    root_problem: { type: 'string', description: 'one sentence: the shared root problem these new PRs address' },
    prs: { type: 'array', items: { type: 'integer' }, minItems: 2 } },
    required: ['root_problem', 'prs'] } },
  standalone: { type: 'array', items: { type: 'integer' } } },
  required: ['joins', 'new_clusters', 'standalone'] }

phase('Index')
const index = await agent(
  `Read the JSON file at ${INDEX_PATH} and return its 'count' (integer), 'units' (array of file path strings), and 'repo' (owner/name string), verbatim.`,
  { label: 'read-index', schema: INDEX_SCHEMA })
log(`${index.count} assignment units`)

phase('Assign')
const results = await parallel(index.units.map((unitPath, i) => () => {
  const outPath = `${OUT_DIR}/${unitPath.split('/').pop()}`
  return agent(
    `Read the JSON file at ${unitPath} — it is {subsystem, existing_clusters:[{id, root_problem, sample_changes}], new_prs:[...]} from ${index.repo}. The new_prs each have primary_change (dominant intent), secondary_changes, one_liner, mechanism, identifiers, and paths.

The existing_clusters are FROZEN: you never modify or re-partition them. For each NEW pr, decide its single PRIMARY home, then optionally any ADDITIONAL clusters it straddles:

- PRIMARY home (exactly one per PR):
  - JOIN an existing cluster — the PR's PRIMARY intent IS that cluster's root problem. Match primary_change against the cluster's root_problem and sample_changes. Emit {pr, cluster_id}.
  - NEW cluster — two or more new_prs share a primary root problem no existing cluster covers. Emit {root_problem, prs:[...]} (>=2 PRs).
  - STANDALONE — shares no primary root problem with any existing cluster or other new PR.
- ADDITIONAL straddle joins (zero or more per PR): if one of the PR's secondary_changes SUBSTANTIALLY advances a DIFFERENT existing cluster's root problem, ALSO emit {pr, cluster_id} for that cluster. A PR that genuinely does two things belongs in both. Most PRs straddle nothing — add an extra join only when the secondary concern clearly delivers that cluster's root fix.

Discipline (same bar as the original clustering):
- Compare on the relevant change (primary for the home, the secondary concern for a straddle), not incidental overlap. Overlapping identifiers/paths are CORROBORATING, not sufficient.
- Bug direction matters: "counts too much" vs "shows too little" are OPPOSITE problems, not the same cluster.
- When unsure, do NOT add a straddle join; never give a PR a primary home it does not clearly fit.
- Every new PR has EXACTLY ONE primary placement (a join, a new-cluster membership, or standalone); straddle joins are extra.

When done, FIRST use the Write tool to save EXACTLY this object as raw JSON (no prose, no wrapping) to the file ${outPath}: {"joins":[...],"new_clusters":[...],"standalone":[...]}. THEN return the same object via structured output.`,
    { label: `assign:unit-${i}`, phase: 'Assign', schema: ASSIGN_SCHEMA })
}))

const ok = results.filter(Boolean)
const sum = (k) => ok.reduce((n, r) => n + r[k].length, 0)
log(`assigned: ${sum('joins')} joins / ${sum('new_clusters')} new clusters / ${sum('standalone')} standalone (durable in ${OUT_DIR}/ — commit with cluster_driver.py commit-assign-dir)`)
return { count: ok.length }
