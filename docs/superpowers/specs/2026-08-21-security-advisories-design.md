# Security advisories in the Alerts family

**Date:** 2026-08-21
**Status:** approved design, awaiting implementation plan

## Problem

GitHub repository security advisories for `TRIAGE_REPO` are invisible to
Prospector. On the triaged repository, ~80 human-reported advisories sit in `triage`
state. Many are duplicates of each other or of published advisories, and
several describe behavior that has since been fixed on the default branch.
Nobody knows which, because reading 80 reports of 6–16k characters each is
the work nobody does.

## Goal (v1)

Ingest every repository advisory as the bot, decide for each open report
whether it is already fixed or a duplicate, and show the result in the app
with evidence. **Read-only upstream.** Closing advisories from the app is a
deliberate follow-up, and the store and verdict shape here are chosen so that
follow-up is one gate and one allowlist entry, not a redesign.

## Non-goals

- Any upstream write (close, accept, publish, comment). The advisory thread
  has no comment API, so a close is silent; that needs its own design.
- Semantic clustering across advisories. The agent names duplicates
  directly; `issue_triage.cluster_issues` is reusable later if that proves
  too weak.
- Changes to the existing alert sources beyond folding their jobs into the
  single sweep.

## How advisories differ from alerts

| | code-scanning / dependabot / secret-scanning | repository advisory |
|---|---|---|
| identity | per-repo integer `number` | `GHSA-xxxx-xxxx-xxxx` |
| states | `open / dismissed / fixed` | `triage / draft / published / closed` |
| content | structured (rule, package, location) | free text report + sparse `vulnerabilities` |
| "handled" means | dismissed or fixed | closed (dup / not a vuln) or published (real, fixed) |

The states are stored under GitHub's own names. No mapping onto the alert
enum: `published` is not `fixed`, and a lie in the state column is what a
later write gate would trip over.

## Design

### Store and model — `alert_triage/advisory_store.py`, `advisory_model.py`

- New `advisories` table through the shared `storekit.Collection`, sitting
  beside `alerts` in the same database. Columns: `id BigInteger` primary
  key, `data JSON`, mirrors `ghsa_id, state, severity, updated_at`, and
  `saved_at`. `schema.STORE_SCHEMA_VERSION` bumps.
- `advisory_id(ghsa: str) -> int`: the twelve symbols after `GHSA-` read as
  a base-21 integer over GitHub's fixed alphabet `23456789cfghjkmpqrvwx`.
  Bijective, so `ghsa_of(id: int) -> str` decodes it; max value ≈ 7.4e15,
  inside 64 bits. Unknown symbols raise `ValueError`.
- Sections and stamping mirror `Alert`: `meta` (stamped `checked_at`),
  `links` and `fix_scan` (stamped `checked_at` + `against_updated_at` =
  `meta.updated_at`). `is_current` reuses `storekit.is_current_core` through
  `alert_freshness` with the same `FIX_SCAN_MAX_AGE_DAYS = 7`.
- `meta` fields: `ghsa_id, state, severity (critical|high|medium|low|unknown),
  summary, description, cve_id, cwe_ids, reporter, author, created_at,
  updated_at, published_at, closed_at, html_url, vulnerable_range,
  patched_versions`. `reporter` is the first `credits` login, else the first
  `collaborating_users` login, else `author`.
- `fix_scan` fields: `verdict ∈ {fixed, likely-fixed, not-fixed, duplicate}`,
  `by ∈ {deterministic, agent}`, `duplicate_of` (a GHSA, required when
  `verdict == duplicate`, forbidden otherwise), `fix_commit` (a default-branch
  SHA, required for `fixed`), `evidence` (short text). Validated on write.
- `links.candidates`: the `link_prs` output shape, text-ref signal only
  (GHSA id and CVE id against open and merged PR bodies). There is no file path to diff-overlap against until an agent
  names one, and v1 does not feed that back.

### Ingest — `alert_triage/advisory_ingest.py`

One module: fetch, normalize, upsert.

- Lists all four states as the bot via `config.gh_alert_read_all`
  (`gh api -X GET --paginate --slurp`, which already follows the cursor
  pagination this endpoint uses). A 403/404 raises `SourceUnavailable`, and
  the sweep records the source as unavailable and continues.
- Normalizes the payload to `meta`. `severity` null → `unknown`.
- Upserts only when `meta` changed (same `_meta_unchanged` test as alerts).
  Recomputes `links` for `triage` and `draft` advisories only; `closed` and
  `published` carry whatever links they had.
- Appends an `advisory-ingest` run record with fetched/unavailable/upserted
  counts.

### Find-fixed — `alert_triage/advisory_find_fixed.py`

One module: pure functions at the top, the agent runner under `main`.

- **Candidates:** `triage` or `draft` advisories with no current `fix_scan`,
  ordered severity desc, then newest `created_at` first.
- **Tier 0 (deterministic, `by="deterministic"`):** a summary matching
  `CVE ID follow-up for existing (GHSA-[\w-]+)` → `duplicate` of the captured
  id. Nothing else is inferred without an agent.
- **Agent wave (`by="agent"`):** the same runner shape as
  `alert_triage/find_fixed.py` — a JSON bundle per batch written to a temp
  file, `headless_agent.run_agent(prompt, allow_gh=True)`, fenced-JSON
  verdicts parsed back, store writes on the calling thread so an abort keeps
  every committed batch. Batches of 4 (descriptions are long). Defaults
  `--limit 12 --concurrency 3`.
- **What each batch receives:** its advisories' full `meta`, plus a roster of
  every advisory's `{ghsa_id, state, summary}` so it can name a
  `duplicate_of`. Duplicate preference order: `published` > `draft` >
  older `triage`. The prompt carries the same untrusted-text warning the
  alert prompt does — the description is reporter-authored.
- **Criteria in the prompt:** `fixed` needs a named commit on the default
  branch that removes or guards the described behavior; `likely-fixed` when
  the described code path no longer exists or is guarded but no single commit
  is identifiable; `duplicate` only for the same root cause at the same
  surface, not the same general area; otherwise `not-fixed`.
- Appends an `advisory-find-fixed` run record.

### One job: `security-sweep`

`prospector_app/backend/jobs.py` replaces the `alert-ingest` and
`alert-find-fixed` entries with one `security-sweep` job that runs, in order,
alert ingest → alert find-fixed → advisory ingest → advisory find-fixed, as
one subprocess (`alert_triage/security_sweep.py`, a thin sequencer calling
the four `main()`s) so the Control tab shows one progress stream and one
button. It takes the agent `--limit` as its one parameter and passes it to
both find-fixed passes. The four CLIs remain runnable on their own.

### Backend — `prospector_app/backend/advisories.py`, `advisory_data.py`

- `advisory_data` is the cached read side, a copy of `alert_data` over the
  new collection (`LazySnapshot`, `advisories_since` watermark, runs).
- Routes: `GET /api/advisories` (all rows), `POST /api/advisories/query`
  (`state`, `verdict` incl. `none`, free-text `q` over ghsa/summary/reporter/
  cve, sort, offset/limit), `GET /api/advisories/{ghsa}` (row + description
  + `fix_scan` + links). No execute route; `safety_guard` is untouched.
- Availability rides the existing `/api/alerts/caps` response as a fourth
  source, probed the same way (one `per_page=1` list as the bot).
- Row shape: `ghsa_id, state, severity, summary, reporter, cve_id, created_at,
  updated_at, html_url, verdict, by, duplicate_of, fix_commit, evidence,
  links, link_count`.

### Frontend — `views/Alerts.tsx`, new `views/Advisories.tsx`

- The 🛡️ Alerts page gains a segmented control at the top, **Advisories |
  Alerts**, Advisories selected by default. The existing alerts table moves
  unchanged under the Alerts segment.
- Advisories table columns: GHSA, state chip, severity chip, summary,
  reporter, age, verdict chip (`duplicate` renders `→ GHSA-…`), links. Default
  filter `state ∈ {triage, draft}`; state and verdict segmented controls;
  one text box.
- Detail panel: rendered description (markdown), verdict + evidence with the
  commit linked upstream, link chips (PRs open the in-app flyout as in
  alerts), and **Open on GitHub**. No other action.
- Types in `api.ts`; `pnpm run build` at 0 errors; no new eslint findings.

## Security and trust

- All reads authenticate as the bot App, which already holds advisory read
  access (a minted token lists advisories today). No write permission is
  needed; the operator can drop `security_advisories: write` from the App.
- Advisory descriptions are confidential until published. They live only in
  the private store and the operator-only app, the same boundary as
  secret-scanning locations today, and never appear in generated markdown
  or in any upstream write.
- The agent reads reporter-authored text; the prompt labels it untrusted and
  the agent has read-only tools.

## Testing

- `alert_triage/tests/test_advisory_store.py`: id bijection over the whole
  alphabet and round-trip, rejection of bad symbols, validation of every
  `fix_scan` shape rule (duplicate needs `duplicate_of`, fixed needs
  `fix_commit`), state/severity enums.
- `test_advisory_ingest.py`: normalizer fields, upsert-on-change, links only
  for open states, `SourceUnavailable` handling.
- `test_advisory_find_fixed.py`: candidate ordering and freshness, the
  tier-0 rule, verdict application, out-of-batch verdicts dropped.
- `prospector_app/backend/tests/test_advisories_api.py`: seeded `tmp_path`
  store, query filters, detail route, 404 on a malformed GHSA.
- `test_jobs.py` updated for `security-sweep`.
- pyright and ruff stay at 0.

## Follow-up (explicitly not in this spec)

Close-as-duplicate from the app: `advisory_gates.close_eligibility`
(duplicate verdict current, canonical GHSA exists and is not itself `triage`,
operator-approved), an `ADVISORY_WRITE_ALLOW` entry for
`PATCH repos/{repo}/security-advisories/GHSA-*`, an executor path mirroring
`dismiss_alert`, and the app showing the comment text for the operator to
paste on the advisory thread.
