export const meta = {
  name: 'propose-pr-clusters',
  description: 'Semantic clustering of summarized PRs, per subsystem chunk',
  phases: [
    { title: 'Index', detail: 'read the unit index' },
    { title: 'Cluster', detail: 'one agent per subsystem chunk' },
  ],
}
// The driver (`cluster_driver.py write-cluster-units`) writes
// /tmp/pipeline-cluster-units/unit-NNN.json + index.json. Each agent reads ONLY
// its own unit file (no giant load-agent echo). Stable-ID matching in
// commit-clusters merges overlapping proposals across chunks.
const INDEX_PATH = '/tmp/pipeline-cluster-units/index.json'
// Where each agent persists its unit's proposals; cluster_driver.py owns this dir
// (CLUSTER_OUT_DIR) and commits it via `commit-clusters-dir`.
const OUT_DIR = '/tmp/pipeline-cluster-out'

const INDEX_SCHEMA = { type: 'object', properties: {
  count: { type: 'integer' }, units: { type: 'array', items: { type: 'string' } },
  repo: { type: 'string', description: 'owner/name of the repository the units came from' } },
  required: ['count', 'units', 'repo'] }

const CLUSTERS_SCHEMA = { type: 'object', properties: { clusters: { type: 'array', items: { type: 'object', properties: {
  root_problem: { type: 'string', description: 'one sentence: the shared root problem or feature goal these PRs all address' },
  prs: { type: 'array', items: { type: 'integer' }, minItems: 2 },
  confidence: { type: 'string', enum: ['high', 'medium', 'low'] } },
  required: ['root_problem', 'prs', 'confidence'] } } }, required: ['clusters'] }

phase('Index')
const index = await agent(
  `Read the JSON file at ${INDEX_PATH} and return its 'count' (integer), 'units' (array of file path strings), and 'repo' (owner/name string), verbatim.`,
  { label: 'read-index', schema: INDEX_SCHEMA })
log(`${index.count} clustering units`)

phase('Cluster')
// Each agent persists its unit's proposals to /tmp/pipeline-cluster-out/ as it
// finishes, so progress is durable per unit and the aggregate never has to
// round-trip (returning every cluster is what truncated large summarize runs).
// The driver — never the agent — writes the store, via commit-clusters-dir.
const results = await parallel(index.units.map((unitPath, i) => () => {
  const outPath = `${OUT_DIR}/${unitPath.split('/').pop()}`
  return agent(
    `Read the JSON file at ${unitPath} — it is {subsystem, part, prs:[...]}, a group of pull-request summaries from ${index.repo} pre-grouped into one subsystem. Each PR has primary_change (its dominant intent, by diffstat weight), secondary_changes (incidental other intents), one_liner, mechanism, identifiers, and paths.

A CLUSTER = two or more PRs whose PRIMARY intent is the SAME root problem or feature (duplicates, competing implementations, or complementary parts of one fix).

Cluster on dominant intent and BUG DIRECTION:
- Compare PRs on their primary_change. A PR whose primary intent differs from a cluster's root problem does NOT belong, even if a secondary_change or a path/identifier incidentally overlaps that cluster.
- Bug direction matters: "counts too much" vs "shows too little" are OPPOSITE problems and are NOT the same cluster, even when they touch the same file.
- Overlapping identifiers/paths are CORROBORATING, not sufficient — they cannot by themselves group two PRs whose primary intents differ. PRs that merely touch the same area are NOT a cluster.

Rules:
- Only group PRs genuinely sharing a primary root problem/feature. When unsure, leave a PR out rather than forcing a fit.
- A PR belongs to at most ONE cluster, chosen by its primary_change.
- root_problem: describe the shared problem concretely (not a category label).
- confidence: high = same primary mechanism/identifiers; medium = same primary problem, different mechanism; low = plausible but summary-level only.
Return a possibly-empty clusters array.

When done, FIRST use the Write tool to save the clusters as a raw JSON array (a list of the cluster objects, nothing else — no wrapping object, no prose; write an empty array [] if there are none) to the file ${outPath}. THEN return the same clusters via structured output.`,
    { label: `cluster:unit-${i}`, phase: 'Cluster', schema: CLUSTERS_SCHEMA })
}))

const all = results.filter(Boolean).flatMap(r => r.clusters)
log(`proposed ${all.length} clusters covering ${all.reduce((n, c) => n + c.prs.length, 0)} PRs (durable in ${OUT_DIR}/ — commit with cluster_driver.py commit-clusters-dir)`)
return { count: all.length }