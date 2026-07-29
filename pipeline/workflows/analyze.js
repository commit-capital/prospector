export const meta = {
  name: 'analyze-pr-clusters',
  description: 'Per-cluster dispositions + outcomes',
  phases: [
    { title: 'Index', detail: 'read the bundle index' },
    { title: 'Analyze', detail: 'one agent per cluster' },
  ],
}
// The driver (`analyze_driver.py write-bundles`) writes /tmp/pipeline-analyze/
// cluster-NNN.json + index.json. Each agent reads ONLY its own cluster bundle.
const INDEX_PATH = '/tmp/pipeline-analyze/index.json'
// Where each agent persists its cluster's analysis; analyze_driver.py owns this dir
// (ANALYZE_OUT_DIR) and commits it via `commit-dir`.
const OUT_DIR = '/tmp/pipeline-analyze-out'

// `prompt` is the canonical ANALYZE decision criteria, owned by analyze_driver.py
// (ANALYZE_PROMPT) and shipped in index.json — consumed here, never restated.
const INDEX_SCHEMA = { type: 'object', properties: {
  count: { type: 'integer' },
  prompt: { type: 'string' },
  clusters: { type: 'array', items: { type: 'object', properties: {
    cluster_id: { type: 'integer' }, path: { type: 'string' } },
    required: ['cluster_id', 'path'] } } }, required: ['count', 'clusters', 'prompt'] }

const ANALYSIS_SCHEMA = { type: 'object', properties: {
  cluster_id: { type: 'integer' },
  outcome: { type: 'string', enum: ['merge-ready', 'awaiting-authors', 'needs-first-party-work', 'close-out', 'blocked-on-decision'] },
  rationale: { type: 'string', description: 'one-paragraph plan: which PR wins and why, what happens to the rest' },
  prs: { type: 'array', items: { type: 'object', properties: {
    pr: { type: 'integer' },
    head_sha: { type: 'string', description: 'copy from the bundle' },
    disposition: { type: 'string', enum: ['merge', 'request-changes', 'close-dup', 'close-fixed', 'close-stale', 'needs-human'] },
    canonical: { type: 'integer', description: 'for close-dup: the kept PR this duplicates' },
    upstream_pr: { type: 'integer', description: 'for close-fixed: upstream PR that already landed the equivalent fix, if known' },
    upstream_commit: { type: 'string', description: 'for close-fixed: upstream commit SHA that already landed the equivalent fix, if known' },
    upstream_date: { type: 'string', description: 'for close-fixed: merge date for the upstream fix, if known (YYYY-MM-DD preferred)' },
    asks: { type: 'array', items: { type: 'string' }, description: 'for request-changes: specific author asks' },
    rationale: { type: 'string' } },
    required: ['pr', 'head_sha', 'disposition', 'rationale'] } } },
  required: ['cluster_id', 'outcome', 'rationale', 'prs'] }

phase('Index')
const index = await agent(
  `Read the JSON file at ${INDEX_PATH} and return 'count' (integer), 'clusters' (array of {cluster_id, path}), and 'prompt' (string) — all verbatim, exactly as written.`,
  { label: 'read-index', schema: INDEX_SCHEMA })
log(`${index.count} clusters to analyze`)

phase('Analyze')
// Each agent persists its cluster's analysis to /tmp/pipeline-analyze-out/ as it
// finishes, so progress is durable per cluster — kill the run anytime and
// `analyze_driver.py commit-dir` lands every finished cluster (then re-run the
// wave for the rest). The driver — never the agent — writes the store.
const results = await parallel(index.clusters.map((c) => () => {
  const outPath = `${OUT_DIR}/${c.path.split('/').pop()}`
  const body = index.prompt.replace('__BUNDLE_PATH__', c.path)
  return agent(
    `${body}

When done, FIRST use the Write tool to save your analysis as a single raw JSON object (the {cluster_id, outcome, rationale, prs:[...]} object, nothing else — no array, no prose) to the file ${outPath}. THEN return the same object via structured output.`,
    { label: `analyze:cluster-${c.cluster_id}`, phase: 'Analyze', schema: ANALYSIS_SCHEMA })
}))

const out = results.filter(Boolean)
log(`analyses produced: ${out.length}/${index.count} (durable in ${OUT_DIR}/ — commit with analyze_driver.py commit-dir)`)
return { count: out.length }
