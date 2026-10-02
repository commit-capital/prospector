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

// `prompt` is the canonical ASSIGN instructions, owned by cluster_driver.py
// (assign_prompt()) and shipped in index.json — consumed here, never restated.
const INDEX_SCHEMA = { type: 'object', properties: {
  count: { type: 'integer' }, units: { type: 'array', items: { type: 'string' } },
  repo: { type: 'string', description: 'owner/name of the repository the units came from' },
  prompt: { type: 'string' } },
  required: ['count', 'units', 'repo', 'prompt'] }

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
  `Read the JSON file at ${INDEX_PATH} and return its 'count' (integer), 'units' (array of file path strings), 'repo' (owner/name string), and 'prompt' (string) — all verbatim, exactly as written.`,
  { label: 'read-index', schema: INDEX_SCHEMA })
log(`${index.count} assignment units`)

phase('Assign')
const results = await parallel(index.units.map((unitPath, i) => () => {
  const outPath = `${OUT_DIR}/${unitPath.split('/').pop()}`
  const body = index.prompt.replace('__UNIT_PATH__', unitPath)
  return agent(
    `${body}

When done, FIRST use the Write tool to save EXACTLY this object as raw JSON (no prose, no wrapping) to the file ${outPath}: {"joins":[...],"new_clusters":[...],"standalone":[...]}. THEN return the same object via structured output.`,
    { label: `assign:unit-${i}`, phase: 'Assign', schema: ASSIGN_SCHEMA })
}))

const ok = results.filter(Boolean)
const sum = (k) => ok.reduce((n, r) => n + r[k].length, 0)
log(`assigned: ${sum('joins')} joins / ${sum('new_clusters')} new clusters / ${sum('standalone')} standalone (durable in ${OUT_DIR}/ — commit with cluster_driver.py commit-assign-dir)`)
return { count: ok.length }
