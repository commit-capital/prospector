export const meta = {
  name: 'pr-security-review',
  description: 'Adversarial security+correctness+scope review of gated merge candidates',
  phases: [
    { title: 'Load', detail: 'read the eligibility manifest' },
    { title: 'Review', detail: '3 lenses per PR' },
    { title: 'Verify', detail: 'chunked refuting verifiers for non-green findings' },
    { title: 'Persist', detail: 'write each verdict to disk as it lands' },
  ],
}
// Input: a manifest file written by `security_driver.py eligible` — the path is
// hardcoded per run because Workflow args do not reliably reach the script.
const MANIFEST_PATH = '/tmp/security-wave.json'
// Each PR's verdict is persisted here the moment it's computed; security_driver.py
// owns this dir (SECURITY_OUT_DIR) and commits it via `commit-dir`.
const OUT_DIR = '/tmp/security-out'

const VERIFY_CHUNK_SIZE = 4

// Fill a shipped prompt template's __PLACEHOLDER__ tokens in ONE pass (token order
// irrelevant; a value that itself contains a token is never re-substituted — safe for
// untrusted PR text). The function replacement inserts each value literally, so a '$'
// in a value isn't treated as a String.replace special. Mirrors headless_agent.fill.
const fill = (tpl, subs) =>
  tpl.replace(new RegExp(Object.keys(subs).join('|'), 'g'), (m) => String(subs[m]))

// `lenses`, `review_prompt`, and `verify_prompt` are the canonical SECURITY review
// text, owned by security_driver.py and shipped in the manifest — consumed here,
// never restated.
const MANIFEST_SCHEMA = { type: 'object', properties: {
  items: { type: 'array', items: { type: 'object', properties: {
    pr: { type: 'integer' }, head_sha: { type: 'string' }, title: { type: 'string' }, diff_path: { type: 'string' } },
    required: ['pr', 'head_sha', 'diff_path'] } },
  lenses: { type: 'array', items: { type: 'object', properties: {
    key: { type: 'string' }, prompt: { type: 'string' } }, required: ['key', 'prompt'] } },
  review_prompt: { type: 'string' }, verify_prompt: { type: 'string' } },
  required: ['items', 'lenses', 'review_prompt', 'verify_prompt'] }

const FINDINGS_SCHEMA = { type: 'object', properties: { lens_summary: { type: 'string' }, findings: { type: 'array', items: { type: 'object', properties: {
  severity: { type: 'string', enum: ['red', 'yellow', 'green'] }, category: { type: 'string' }, title: { type: 'string' },
  location: { type: 'string' }, detail: { type: 'string' }, confidence: { type: 'string', enum: ['high', 'medium', 'low'] } },
  required: ['severity', 'category', 'title', 'detail'] } } }, required: ['findings', 'lens_summary'] }

const VERIFY_BATCH_SCHEMA = { type: 'object', properties: { results: { type: 'array', items: { type: 'object', properties: {
  index: { type: 'integer' }, upheld: { type: 'boolean' },
  adjusted_severity: { type: 'string', enum: ['red', 'yellow', 'green', 'not-an-issue'] },
  reasoning: { type: 'string' } }, required: ['index', 'upheld', 'adjusted_severity', 'reasoning'] } } },
  required: ['results'] }

phase('Load')
const manifest = await agent(
  `Read the JSON file at ${MANIFEST_PATH} and return its full contents — the 'items' array, the 'lenses' array, and the 'review_prompt' and 'verify_prompt' strings — all verbatim, exactly as written.`,
  { label: 'load-manifest', schema: MANIFEST_SCHEMA })
const LENSES = manifest.lenses
log(`reviewing ${manifest.items.length} merge candidates`)

const results = await pipeline(manifest.items,
  (pr) => parallel(LENSES.map(L => () =>
    agent(fill(manifest.review_prompt, {
      '__DIFF_PATH__': pr.diff_path, '__LENS__': L.key, '__LENS_PROMPT__': L.prompt,
      '__PR__': pr.n ?? pr.pr, '__TITLE__': pr.title }),
      { label: `rev:${pr.pr}:${L.key}`, phase: 'Review', schema: FINDINGS_SCHEMA }
      // agent() returns null on terminal API error / skip; a real result has a findings array.
      // Mark each lens ok/failed so a wiped-out run can't masquerade as a clean GREEN.
    ).then(r => (r && Array.isArray(r.findings))
      ? { lens: L.key, ok: true, findings: r.findings, lens_summary: r.lens_summary }
      : { lens: L.key, ok: false, findings: [] })
  )).then(lr => ({ pr, lensResults: lr.filter(Boolean) })),
  async (rev) => {
    // Coverage gate: a GREEN is only trustworthy if every lens actually ran.
    // Zero-or-partial successful lenses must NOT yield GREEN — they map to
    // INCOMPLETE so the driver holds the PR (stays security-eligible, re-runs).
    const lensesOk = rev.lensResults.filter(l => l.ok).length
    const coverageOk = lensesOk === LENSES.length
    const settle = (verdict) => (verdict === 'GREEN' && !coverageOk) ? 'INCOMPLETE' : verdict
    const base = { pr: rev.pr.pr, head_sha: rev.pr.head_sha, lenses_ok: lensesOk, lenses_total: LENSES.length }
    // Per-PR durability: a write agent persists the verdict the moment it's
    // computed (the workflow can't touch the fs; the verdict is JS-assembled from
    // the lens+verify agents, so no single review agent can self-persist it). Kill
    // the run anytime and `security_driver.py commit-dir` lands every finished PR.
    // The driver — never the agent — writes the store.
    const persist = (v) => agent(
      `Use the Write tool to save EXACTLY this JSON object to the file ${OUT_DIR}/pr-${v.pr}.json — verbatim, the entire object, no changes and no prose around it:\n${JSON.stringify(v)}`,
      { label: `persist:${v.pr}`, phase: 'Persist' })
    const flagged = []
    for (const lr of rev.lensResults)
      for (const f of (lr.findings || []))
        if (f.severity !== 'green') flagged.push({ lens: lr.lens, ...f })
    if (!flagged.length) {
      const green = { ...base, verdict: settle('GREEN'), findings: [] }
      await persist(green)
      return green
    }
    const indexed = flagged.map((f, index) => ({ index, ...f }))
    const chunks = []
    for (let i = 0; i < indexed.length; i += VERIFY_CHUNK_SIZE)
      chunks.push(indexed.slice(i, i + VERIFY_CHUNK_SIZE))
    const verified = (await parallel(chunks.map(chunk => () =>
      agent(fill(manifest.verify_prompt, {
        '__N__': chunk.length, '__PR__': rev.pr.pr, '__DIFF_PATH__': rev.pr.diff_path,
        '__CHUNK__': JSON.stringify(chunk) }),
        { label: `vfy:${rev.pr.pr}:${chunk[0].index}-${chunk[chunk.length - 1].index}`, phase: 'Verify', schema: VERIFY_BATCH_SCHEMA }
      ).then(verify => {
        const byIndex = new Map((verify.results || []).map(v => [v.index, v]))
        return chunk.map(f => ({ finding: f, verdict: byIndex.get(f.index) })).filter(x => x.verdict)
      })
    ))).flat()
    const conf = verified.filter(Boolean).filter(x => x.verdict.upheld && x.verdict.adjusted_severity !== 'not-an-issue')
    const keep = (sev) => conf.filter(x => x.verdict.adjusted_severity === sev)
      .map(x => ({ ...x.finding, severity: sev, detail: `${x.finding.detail}\n\n[verified: ${x.verdict.reasoning}]` }))
    const reds = keep('red'), yellows = keep('yellow')
    // RED/YELLOW stand regardless of coverage (a confirmed finding is real);
    // only a clean bill (GREEN) is downgraded when a lens failed to run.
    const verdict = reds.length ? 'RED' : (yellows.length ? 'YELLOW' : 'GREEN')
    const result = { ...base, verdict: settle(verdict), findings: [...reds, ...yellows] }
    await persist(result)
    return result
  }
)

const out = results.filter(Boolean)
const n = (v) => out.filter(r => r.verdict === v).length
log(`verdicts: ${n('GREEN')} GREEN / ${n('YELLOW')} YELLOW / ${n('RED')} RED / ${n('INCOMPLETE')} INCOMPLETE (durable in ${OUT_DIR}/ — commit with security_driver.py commit-dir)`)
return { count: out.length }
