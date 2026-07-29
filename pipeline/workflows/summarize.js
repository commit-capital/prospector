export const meta = {
  name: 'summarize-pr-diffs',
  description: 'Per-PR diff summaries for clustering',
  phases: [
    { title: 'Index', detail: 'read the batch index' },
    { title: 'Summarize', detail: 'one agent per pre-split batch file' },
  ],
}
// The driver (`cluster_driver.py write-batches`) pre-splits the wave into
// /tmp/pipeline-batches/batch-NNN.json + index.json. Each agent reads ONLY its
// own batch file — no giant load-agent echo, so this scales to thousands of PRs.
const INDEX_PATH = '/tmp/pipeline-batches/index.json'
// Where each agent persists its slice; cluster_driver.py owns this dir (SUMMARY_OUT_DIR)
// and commits it via `commit-summaries-dir`.
const OUT_DIR = '/tmp/pipeline-out'

// `prompt` is the canonical SUMMARIZE instructions and `subsystems` the accepted
// vocabulary — both owned by cluster_driver.py (summarize_prompt() / the active
// repository profile) and shipped in index.json — consumed here, never restated.
const INDEX_SCHEMA = { type: 'object', properties: {
  count: { type: 'integer' }, prompt: { type: 'string' },
  subsystems: { type: 'array', items: { type: 'string' } },
  batches: { type: 'array', items: { type: 'string' } } },
  required: ['count', 'batches', 'prompt', 'subsystems'] }

const summariesSchema = (subsystems) => ({ type: 'object', properties: { items: { type: 'array', items: { type: 'object', properties: {
  pr: { type: 'integer' }, head_sha: { type: 'string' },
  one_liner: { type: 'string', description: 'the PRIMARY behavior change, one sentence, mechanism-level not title-level' },
  mechanism: { type: 'string', description: 'HOW: key functions/fields touched and the approach' },
  subsystem: { type: 'string', enum: subsystems },
  identifiers: { type: 'array', items: { type: 'string' }, description: 'distinctive code identifiers from the diff, primary change first, up to 8' },
  paths: { type: 'array', items: { type: 'string' }, description: 'the 1-5 most significant changed file paths, primary change first' },
  primary_change: { type: 'string', description: 'the single dominant behavior change, judged by diffstat weight (largest hunks / most LOC)' },
  secondary_changes: { type: 'array', items: { type: 'string' }, description: 'other distinct intents the PR also makes, demoted not erased; at most the 3 most significant' } },
  required: ['pr', 'head_sha', 'one_liner', 'mechanism', 'subsystem', 'identifiers', 'paths', 'primary_change'] } } }, required: ['items'] })

phase('Index')
const index = await agent(
  `Read the JSON file at ${INDEX_PATH} and return its 'count' (integer), 'batches' (array of file path strings), 'prompt' (string), and 'subsystems' (array of strings) — all verbatim, exactly as written.`,
  { label: 'read-index', schema: INDEX_SCHEMA })
log(`${index.count} batch files to summarize`)

phase('Summarize')
// Each agent persists its own slice to /tmp/pipeline-out/ the moment it finishes,
// so progress is durable at batch granularity: if the run dies mid-pass the
// completed batches are already on disk and `cluster_driver.py commit-summaries-dir`
// lands them (the driver — never the agent — writes the store). The structured
// return is kept only for the run's count log.
const results = await parallel(index.batches.map((batchPath, bi) => () => {
  const outPath = `${OUT_DIR}/${batchPath.split('/').pop()}`
  const body = index.prompt.replace('__BATCH_PATH__', batchPath)
  return agent(
    `${body}

When all items are ready, FIRST use the Write tool to save them as a raw JSON array (a list of the item objects, nothing else — no wrapping object, no prose) to the file ${outPath}. THEN return the same items via structured output.`,
    { label: `summarize:batch-${bi}`, phase: 'Summarize', schema: summariesSchema(index.subsystems) })
}))

// The slices on disk are the durable artifact the driver commits; the return is
// just a count (returning every item is what truncated on large waves).
const count = results.filter(Boolean).reduce((n, r) => n + r.items.length, 0)
log(`summaries produced: ${count} (durable in ${OUT_DIR}/ — commit with cluster_driver.py commit-summaries-dir)`)
return { count }