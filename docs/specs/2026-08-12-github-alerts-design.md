# GitHub Security & Quality Alerts — design

Date: 2026-08-12
Status: approved (design), pending implementation

## Goal

Ingest the private "security and quality" alert surfaces of `TRIAGE_REPO` —
GitHub **code scanning** alerts (CodeQL/SARIF, covering both security and
quality findings), **Dependabot** alerts, and **secret scanning** alerts — into
the Prospector store; show them in a new **🛡️ Alerts** tab; run a tiered pass
that determines which alerts are already fixed and links candidate PRs/issues
to each alert; and let a human dismiss/resolve alerts upstream through the
gated, logged executor path.

## Non-goals (v1)

- No clustering of alerts. Alerts are a flat list with links out to PRs/issues.
- No chat-agent integration (no alert context builder, no chat alert writes).
- No auto-filing of tracking issues for alerts.
- No coupling with `pipeline/threats.py`. The PR threat scan is about incoming
  diffs; GitHub secret scanning is about secrets already in the repository.
  They stay separate mechanisms; the UI labels the new source
  "GitHub secret scanning" so the distinction is legible.
- No automatic dismissal. Every upstream alert write is human-approved.

## Vocabulary

- **Alert**: one GitHub alert from one source. `source ∈ {code-scanning,
  dependabot, secret-scanning}`; identity is `(source, number)`.
- **Normalized state**: `open | dismissed | fixed`. GitHub's per-source raw
  state (`resolved`, `auto_dismissed`, …) is preserved in `meta.raw_state` and
  mapped: code scanning `open/dismissed/fixed` as-is; Dependabot
  `auto_dismissed → dismissed`; secret scanning `resolved → fixed` when the
  resolution is `revoked`, else `dismissed` (resolutions `false_positive`,
  `wont_fix`, `used_in_tests`, `pattern_*`).
- **Fix-scan verdict**: `fixed | likely-fixed | not-fixed` (mirrors
  `issue_triage` `FIX_SCAN_STATES`).
- **Suggested action**: `dismiss-fixed | needs-fix | needs-human`.
- **Link candidate**: `{kind: pr|issue, number, how, note}` where
  `how ∈ {upstream-state, manifest-bump, diff-overlap, text-ref, agent}`.

## Architecture

A new top-level package **`alert_triage/`**, a full parallel of
`issue_triage/`, over the same shared `storekit` core and `pipeline/schema.py`
metadata. Reads and writes authenticate as the configured GitHub App
(`TRIAGE_BOT_LOGIN`) via the existing `pipeline/get-bot-token.sh` minting path
— no new secret. The App must be granted repository permissions upstream:
Code scanning alerts (read/write), Dependabot alerts (read/write), Secret
scanning alerts (read/write). A deployment that has not granted a permission
sees that source as unavailable; nothing errors loudly on the read path.

### Store (shared `pipeline/schema.py`)

One new table:

```
alerts (
  id        Integer PK    -- SOURCE_ORDINAL * 10_000_000 + alert number
  data      JSON          -- full validated record
  source    String idx    -- code-scanning | dependabot | secret-scanning
  number    Integer idx   -- GitHub's per-source alert number
  state     String idx    -- normalized state
  severity  String idx    -- normalized severity
  updated_at String       -- upstream alert updated_at
  saved_at  String idx    -- watermark write-stamp
)
```

`SOURCE_ORDINAL`: code-scanning=1, dependabot=2, secret-scanning=3. Alert
numbers are per-repo sequential integers, far below 10M; the synthetic id is
deterministic and stable, so re-ingest upserts in place. The `runs` table
gains ledger kind `"alert"`. `STORE_SCHEMA_VERSION` bumps to 11 (new table +
new runs kind; older writers mishandle neither, but the bump keeps the
changelog honest and guards section-shape drift).

Record sections (`ALERT_SECTIONS`):

- `meta` — source, number, raw_state, state, severity, created_at, updated_at,
  html_url, plus per-source identity:
  - code scanning: `rule_id`, `rule_description`, `tool`, `security_severity`
    (may be null for pure-quality findings), `path`, `start_line`, `end_line`,
    `ref` (from `most_recent_instance`), `dismissed_reason/at/by` when present.
  - dependabot: `package`, `ecosystem`, `manifest_path`, `ghsa_id`, `cve_id`,
    `vulnerable_range`, `fixed_version`, `dependency_scope`.
  - secret scanning: `secret_type`, `secret_type_display_name`, `locations`
    (paths + line spans only), `resolution` when present. **The secret value is
    never fetched, stored, or displayed.**
- `links` — `{candidates: [link candidate…]}`, deterministic + agent-found.
- `fix_scan` — `{verdict, action, evidence, by: deterministic|agent}`.

Severity normalization: code scanning uses `security_severity_level` when
present else the rule severity mapped (`error→high`, `warning→medium`,
`note→low`); Dependabot uses the advisory severity; secret scanning is always
`critical`. Normalized set: `critical | high | medium | low`.

### Freshness

`alert_triage/alert_freshness.py` mirrors `issue_freshness.py`: every fact
section (`links`, `fix_scan`) is stamped `against_updated_at` with the alert's
upstream `updated_at`. An alert whose `updated_at` moves (state change, new
instance) auto-stales its facts; `is_current()` is the single check. The
fixed-pass additionally respects a max-age (`FIX_SCAN_MAX_AGE_DAYS = 7`)
so verdicts refresh even when the alert itself is quiet, since the *repo*
moving is what fixes alerts.

### Package modules (`alert_triage/`)

- `config.py` — `gh_alert_read(path, params)`: `gh api -X GET` with an env
  carrying `GH_TOKEN=<minted bot token>` (the `-X GET` is the hard read-only
  guarantee, as in `issue_triage/config.py`). `mint_token()` invokes
  `pipeline/get-bot-token.sh` in a subprocess — the same script the executor
  uses — so CLI-run drivers need no import from the app backend; fetch
  functions take the minted `token: str` explicitly.
- `fetch_alerts.py` — paginated fetch per source
  (`/repos/{repo}/code-scanning/alerts?state=…&per_page=100`, same for
  `dependabot/alerts`, `secret-scanning/alerts`), fetching **all states** so
  upstream-fixed alerts land as tier-0 truth; per-source `normalize_*`
  functions producing validated records. A 403/404 per source returns a typed
  `SourceUnavailable` marker, not an exception blast.
- `alert_store.py` — `AlertStore` over `storekit.Collection`, vocabularies
  (`ALERT_SOURCES`, `ALERT_STATES`, `ALERT_SEVERITIES`, `FIX_SCAN_STATES`,
  `ALERT_ACTIONS`, `ALERT_SECTIONS`), `validate_alert`, `alert_id(source,
  number)`, `batch()`, `append_run` (kind `"alert"`), `alerts_since` for the
  app snapshot.
- `alert_model.py` — `Alert` domain wrapper with `_stamp`-based mutators
  (`record_links`, `record_fix_scan`, `record_meta`) that auto-persist.
- `alert_gates.py` — the ONE policy module for alerts:
  `dismiss_eligibility(alert, reason, today) -> tuple[bool, str]`. Rules:
  alert must be `open`; `dismiss-fixed`-family reasons require a current
  `fix_scan` verdict of `fixed` or `likely-fixed`; `false positive` /
  `won't fix` / `used in tests` require a non-empty operator note (always
  allowed with one — human judgment, logged). Fail-closed: no verdict, stale
  verdict, or unknown reason ⇒ ineligible.
- `alert_ingest.py` — the ingest driver: fetch all three sources (skipping
  unavailable ones with a recorded note), upsert rows, ledger entry
  `{"phase": "alert-ingest", stats per source}`. Idempotent, cheap,
  re-runnable.
- `link_prs.py` — deterministic link finding over store PRs/issues:
  Dependabot ⇒ PRs whose title/body mention the package (and dependency
  manifests among changed files when diff data is cached); code scanning ⇒
  PRs whose cached diff touches the alert's `path`; all sources ⇒ PR/issue
  text mentioning the rule id, GHSA/CVE id, package name, or secret type.
  Capped per alert (`LINK_CAP = 8`), evidence-tiered like
  `issue_triage/link_prs.py`.
- `alert_fixed_driver.py` — the tiered fixed-pass driver (mirrors
  `issue_fixed_driver.py`): `candidates()` (open alerts with absent/stale
  `fix_scan`), `deterministic_fixed()` (tier 0), `bundle()` (evidence pack for
  one alert: meta, rule description, code excerpt at HEAD when the path
  exists, candidate PR diffs, candidate issues), the canonical
  `FIND_FIXED_PROMPT` + criteria, and `apply_verdicts()` writing
  `fix_scan`/`links` + a ledger entry. Tier 0 rules:
  - upstream state `fixed` ⇒ verdict `fixed`, link `upstream-state` (no agent).
  - Dependabot: a merged PR bumping the same package in the same manifest to
    ≥ `fixed_version` ⇒ `likely-fixed`; an open one ⇒ link `manifest-bump`,
    verdict stays for the agent.
  - code scanning: alert path deleted at current default-branch HEAD ⇒
    `likely-fixed`.
- `find_fixed.py` — the agent wave runner over `pipeline/headless_agent.py`,
  parallel like `issue_triage/find_fixed.py`: each agent gets one bundle,
  returns `{verdict, action, evidence, links}` as strict JSON; the driver
  validates and stores verbatim.
- `tests/` — see Testing.

### App backend (`prospector_app/backend/`)

- `alert_data.py` — `LazySnapshot` over `alert_store.alerts_since` +
  `runs_since(kind="alert")`, exactly the `issue_data.py` shape.
- `alerts.py` — row projection (`_row`), `list_alerts`, `query_alerts`
  (filters: source, state, severity, verdict, linked, text), `alert_detail`.
- Routes in `app.py` (literal routes before parameterized, per the existing
  `/api/issues` note): `GET /api/alerts`, `POST /api/alerts/query`,
  `GET /api/alerts/{source}/{number}`,
  `POST /api/alerts/{source}/{number}/dismiss`.
- Dismiss execution: a dedicated executor function `alert_dismiss_run` in
  `executor.py`, allowlisted in `safety_guard.py` (`ALERT_WRITE_ALLOW`)
  permitting exactly the three shapes
  `gh api -X PATCH repos/{TRIAGE_REPO}/(code-scanning|dependabot|secret-scanning)/alerts/{n}`
  as the bot, refusing any write with an empty token, forced dry-run when
  minting fails, gated by `alert_gates.dismiss_eligibility`, recorded in
  Activity (new kinds `alert-dismiss`, one per attempt including dry-runs).
  Secret scanning uses `state=resolved` + `resolution=…`; the other two use
  `state=dismissed` + `dismissed_reason=…` (+ comment where supported).
- `caps.py` — `alerts` capability: `{configured, sources: {code-scanning:
  bool, dependabot: bool, secret-scanning: bool}}`, probed with a 1-item read
  per source under the minted token; all-false or minting-unavailable ⇒ the
  tab renders an explanatory empty state.
- `jobs.py` — `JOB_SPECS` entries `alert-ingest` and `alert-fixed` so both run
  from the Control tab with streamed output; `pipeline_status.py` phase
  labels for the two ledger phases.
- `activity.py` — new kinds registered.

### Frontend (`prospector_app/frontend/`)

- `views/Alerts.tsx` modeled on `views/Issues.tsx`: paginated sorted table —
  source chip, severity chip, identity (rule / package / secret type), state,
  fix-scan verdict chip, linked PRs/issues, updated age. Column filters via
  the shared filter components.
- Detail flyout (query-param driven like `?issue=`): full meta, evidence,
  links, and the dismiss form — reason picker (per-source valid reasons),
  optional comment, confirm; disabled with the gate's reason when ineligible.
- One route in `main.tsx`, one `🛡️ Alerts` NavLink + `VIEW_NAMES` entry in
  `App.tsx`, `api.ts` types + methods.

### Config surface

- `pyproject.toml`: `packages += alert_triage`; `testpaths += alert_triage/tests`.
- `.github/workflows/pipeline-tests.yml`: pyright command += `alert_triage`.
- `CLAUDE.md` / `README.md` / `ARCHITECTURE.md`: document the family, the App
  permission requirements, and the secret-scanning-vs-threat-scan distinction.

## Error handling

- Token minting fails ⇒ ingest exits with the mint error recorded; dismiss is
  dry-run-forced; caps says unavailable. Never a fallback to the operator
  login for alert reads or writes.
- Per-source 403/404 ⇒ that source marked unavailable this run; others
  proceed.
- Agent output failing validation ⇒ that alert's verdict is skipped (left
  absent/stale), never a partial write.
- `assert_push_target`-style scoping: the dismiss path pins `TRIAGE_REPO` and
  the three literal endpoint shapes; nothing else passes the allowlist.

## Testing

- `alert_triage/tests/`: normalization fixtures per source (including
  secret-value-absence assertion), synthetic-id stability, state/severity
  mapping matrix, freshness staleness on `updated_at` moves and max-age,
  tier-0 deterministic rules, link finding, `dismiss_eligibility` matrix,
  ingest idempotency against a local SQLite store.
- `prospector_app/backend/tests/`: row projection, query filters, dismiss
  route gating (ineligible ⇒ 4xx with reason; empty token ⇒ refused),
  safety-guard allowlist accepts exactly the three shapes and refuses
  variants.
- Frontend: `pnpm run build` (tsc 0 errors) + no new lint errors.
- `uv run pyright pipeline issue_triage alert_triage prospector_app/backend
  review-new-pr/harness` at 0 errors; `uv run ruff check .` clean.
