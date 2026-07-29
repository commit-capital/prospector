// pipeline/workflows/greptile_read.js
export const meta = {
  name: 'greptile-read',
  description: 'Semantic nit-vs-defect classification of Greptile findings',
  phases: [
    { title: 'Index', detail: 'read the batch index' },
    { title: 'Classify', detail: 'one agent per batch' },
  ],
}
const INDEX_PATH = '/tmp/pipeline-greptile-batches/index.json'
const OUT_DIR = '/tmp/pipeline-greptile-out'

const INDEX_SCHEMA = { type: 'object', properties: {
  count: { type: 'integer' }, prompt: { type: 'string' },
  batches: { type: 'array', items: { type: 'string' } } },
  required: ['count', 'batches', 'prompt'] }

const RESULT_SCHEMA = { type: 'object', properties: { items: { type: 'array', items: { type: 'object', properties: {
  pr: { type: 'integer' }, head_sha: { type: 'string' },
  severity: { type: 'string', enum: ['defects', 'nits', 'clean'] },
  findings: { type: 'array', items: { type: 'object', properties: {
    headline: { type: 'string' },
    class: { type: 'string', enum: ['substantive', 'nitpick'] },
    why: { type: 'string' } }, required: ['headline', 'class', 'why'] } },
  summary: { type: 'string' } },
  required: ['pr', 'head_sha', 'severity', 'findings', 'summary'] } } }, required: ['items'] }

phase('Index')
const index = await agent(
  `Read the JSON file at ${INDEX_PATH} and return its 'count', 'batches', and 'prompt' verbatim.`,
  { label: 'read-index', schema: INDEX_SCHEMA })
log(`${index.count} batch files to classify`)

phase('Classify')
const results = await parallel(index.batches.map((batchPath, bi) => () => {
  const outPath = `${OUT_DIR}/${batchPath.split('/').pop()}`
  const body = index.prompt.replace('__BATCH_PATH__', batchPath)
  return agent(
    `${body}

FIRST use the Write tool to save the items as a raw JSON array (list of item objects, no wrapper) to ${outPath}. THEN return the same items via structured output.`,
    { label: `greptile-read:batch-${bi}`, phase: 'Classify', schema: RESULT_SCHEMA })
}))
const total = results.filter(Boolean).reduce((a, r) => a + r.items.length, 0)
log(`classified ${total} PRs`)
return { classified: total }
