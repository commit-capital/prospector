export const meta = {
  name: 'straddle-clustered-prs',
  description: 'Backfill: add secondary-concern memberships to already-clustered PRs',
  phases: [
    { title: 'Index', detail: 'read the unit index' },
    { title: 'Straddle', detail: 'one agent per subsystem of clustered PRs' },
  ],
}
// The driver (`cluster_driver.py write-straddle-units`) writes
// /tmp/pipeline-straddle-units/unit-NNN.json + index.json. Each unit is
// {subsystem, existing_clusters, prs:[... + current_clusters]}. Output is the
// assign `joins` shape and is committed via `cluster_driver.py commit-assign-dir`.
const INDEX_PATH = '/tmp/pipeline-straddle-units/index.json'
const OUT_DIR = '/tmp/pipeline-assign-out'

const INDEX_SCHEMA = { type: 'object', properties: {
  count: { type: 'integer' }, units: { type: 'array', items: { type: 'string' } },
  repo: { type: 'string', description: 'owner/name of the repository the units came from' } },
  required: ['count', 'units', 'repo'] }

const STRADDLE_SCHEMA = { type: 'object', properties: {
  joins: { type: 'array', items: { type: 'object', properties: {
    pr: { type: 'integer' }, cluster_id: { type: 'integer' } },
    required: ['pr', 'cluster_id'] } } },
  required: ['joins'] }

phase('Index')
const index = await agent(
  `Read the JSON file at ${INDEX_PATH} and return its 'count' (integer), 'units' (array of file path strings), and 'repo' (owner/name string), verbatim.`,
  { label: 'read-index', schema: INDEX_SCHEMA })
log(`${index.count} straddle units`)

phase('Straddle')
const results = await parallel(index.units.map((unitPath, i) => () => {
  const outPath = `${OUT_DIR}/${unitPath.split('/').pop()}`
  return agent(
    `Read the JSON file at ${unitPath} — it is {subsystem, existing_clusters:[{id, root_problem, sample_changes}], prs:[...]} from ${index.repo}. Each pr is ALREADY clustered (its current_clusters lists the cluster ids it belongs to) and has primary_change, secondary_changes, one_liner, mechanism, identifiers, and paths.

The existing_clusters are FROZEN. Your ONLY job: for each pr, decide whether one of its secondary_changes ALSO makes it belong to an existing cluster it is NOT already in. This is a STRADDLER pass — most PRs add nothing.

For each pr, emit {pr, cluster_id} for every ADDITIONAL existing cluster (from existing_clusters, NOT already in its current_clusters) whose root_problem is SUBSTANTIALLY advanced by one of the pr's secondary_changes. A PR that genuinely does two things belongs in both clusters.

Discipline (same bar as the original clustering):
- Match the SECONDARY concern against the candidate cluster's root_problem and sample_changes — not the pr's primary intent (its primary home is already set), and not incidental overlap. Overlapping identifiers/paths are CORROBORATING, not sufficient.
- Bug direction matters: "counts too much" vs "shows too little" are OPPOSITE problems.
- NEVER propose a cluster already in the pr's current_clusters.
- When unsure, add nothing. Emit no row for a pr that straddles nothing — an empty joins array is the expected, common result.

When done, FIRST use the Write tool to save EXACTLY this object as raw JSON (no prose, no wrapping) to the file ${outPath}: {"joins":[...]}. THEN return the same object via structured output.`,
    { label: `straddle:unit-${i}`, phase: 'Straddle', schema: STRADDLE_SCHEMA })
}))

const ok = results.filter(Boolean)
const joins = ok.reduce((n, r) => n + r.joins.length, 0)
log(`proposed ${joins} additional memberships across ${ok.length} units (durable in ${OUT_DIR}/ — commit with cluster_driver.py commit-assign-dir)`)
return { count: ok.length, joins }
