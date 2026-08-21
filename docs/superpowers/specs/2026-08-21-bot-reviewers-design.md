# Bot reviewers: every automated PR reviewer and scanner, detected and gated

**Date:** 2026-08-21
**Status:** design approved pending implementation plan

## Problem

Prospector reads exactly one automated PR reviewer — Greptile — and reads it by
name. `review_policy` knows two profiles (`greptile` | `none`), `ingest` fetches
a score only when the configured provider is Greptile, the semantic-read phase,
the fix goal, the analyze/chat prompts, the PR Explorer column, the filters, the
PR page and the retrigger path all spell "Greptile". Anything else that reviews
or scans a PR on the triaged repository is invisible, or collapses into the CI
pass/fail signal.

The triaged repository today (`paperclipai/paperclip`, checked 2026-08-21) runs:

| bot | what it posts | currently |
|---|---|---|
| Greptile (`greptile-apps[bot]`) | issue comment with `Confidence Score: N/5` + `Last reviewed commit`; PR reviews with inline comments; a `Greptile Review` check run whose title carries the score and which **fails** below the repo's 5/5 threshold | read as the one review provider; its failing check run also makes CI read `failing` |
| CodeRabbit (`coderabbitai[bot]`) | PR reviews (`Actionable comments posted: N`, `commit_id` = reviewed head) with inline findings tagged `🔴 Critical` / `🟠 Major` / `🟡 Minor` / `🧹 Nitpick`; a walkthrough issue comment with pre-merge checks (`✅ 4 ❌ 1`) | invisible (it reviewed 184 PRs 2026-03-05 → 2026-06-18, then went dormant) |
| Superagent (`superagent-security[bot]`) | PR reviews `Superagent found N security concern(s)` with inline `**P1:**` / `**P2:**` findings (`<!-- brin-pr-finding -->`); check runs `Superagent Security Scan` (`action_required` when concerns exist), `Superagent Supply Chain Scan` (neutral = inconclusive), `Contributor trust` (`Score: 89/100 · Verdict: safe`) | invisible except that `action_required` makes CI `failing` |
| Socket (`socket-security[bot]`) | issue comment table of dependency changes; check runs `Socket Security: Pull Request Alerts` (`failure` on new alerts, `neutral` when skipped) and `Socket Security: Project Report` | invisible except through CI |

The ask: see which bots are running on the target repository, ingest all of
their feedback, use it everywhere a PR's quality or safety is judged (a PR is
not green while an active reviewer or scanner objects), and surface it on the
PR page and in PR Explorer columns/filters.

Decisions taken with the operator:

- **Every active code reviewer gates `pr_clean`.** A provider that goes dormant
  on the repository stops gating automatically; its old data stays visible.
- **Scanner findings block `pr_clean` under their own name** (never hidden in
  "CI failing") and are handed to the SECURITY and ANALYZE agents as evidence.
  They do not flip disposition by themselves.
- **Active providers are auto-detected from the repository's PR data;**
  `TRIAGE_REVIEW_PROVIDER` stays as an override (`auto` | `none` | explicit list).

## Vocabulary

- **Reviewer** — an automated bot that posts feedback on PRs. Two **kinds**:
  `review` (code-review providers: Greptile, CodeRabbit) and `scanner`
  (security scanners: Superagent, Socket). The kind decides which gate reads it
  and which agents receive it.
- **Entry** — one reviewer's normalized feedback on one PR, stored under
  `reviews[<reviewer id>]`.
- **Bar** — a reviewer's pass condition, evaluated by its adapter:
  `pass | fail | stale | pending | na`.
- **Active** — a reviewer gates a repository's PRs when it is active there:
  configured explicitly, or auto-detected as having posted on a PR head within
  the activity window.

## Architecture

### `pipeline/reviewers.py` — the ONE reviewer registry (new)

A frozen `Reviewer` dataclass per known bot and a module-level registry
`REVIEWERS: dict[str, Reviewer]`:

```python
@dataclass(frozen=True)
class Reviewer:
    id: str                     # "greptile" | "coderabbit" | "superagent" | "socket"
    label: str                  # "Greptile", "CodeRabbit", "Superagent", "Socket"
    kind: str                   # "review" | "scanner"
    logins: tuple[str, ...]     # login substrings: ("greptile",), ("coderabbitai",), ...
    app_slugs: tuple[str, ...]  # check-run app slugs: ("greptile-apps",), ("coderabbitai",), ...
    retrigger_mention: str | None   # "@greptileai", "@coderabbitai review", None, None
    score_max: int | None           # 5 for Greptile; None otherwise
```

Per-reviewer adapter functions, dispatched by id:

- `parse(reviewer, feed: PrFeed, head_sha) -> dict | None` — the normalized
  entry from a raw feed (below), `None` when the bot left nothing on the PR.
- `bar(reviewer, entry: dict | None, head_sha, *, threshold) -> Bar` where
  `Bar(status, reason, ask)`; `status ∈ pass|fail|stale|pending|na`. The
  `reason` is the short operator-facing clause that lands in `pr_clean`
  reasons; `ask` is the author-facing sentence for `bar_asks`.
- `severity(reviewer, entry, pr) -> str | None` — `defects | nits | clean` for
  `review`-kind reviewers (Greptile from the current `greptile_review` read;
  CodeRabbit from its own markers).
- `digest(reviewer, entry, bar) -> dict` — the compact row projection the app,
  chat, analyze bundle, deep-search and training read.
- `findings_for_fix(reviewer, entry, head_sha) -> list[dict]` — open,
  current, substantive findings in the `{headline, class, why, path, line}`
  shape `author_fix._findings_block` renders (scanners return `[]`: a P1
  exfiltration finding is never an autofix goal).

**Normalized entry shape** (stored verbatim under `reviews[id]`):

```
{
  "kind": "review" | "scanner",
  "reviewed_sha": str | None,   # commit the bot last reviewed (review commit oid /
                                #   check-run head); None when unknowable
  "observed_at": iso,           # the bot's latest activity on this PR
  "score": int | None,          # Greptile confidence; None for others
  "findings": [                 # inline review threads the bot opened
    {"path", "line", "severity", "title", "body", "resolved": bool,
     "outdated": bool, "commit": sha, "url"}
  ],
  "summary": str | None,        # the bot's own summary text, HTML-stripped,
                                #   capped (Greptile score comment, CodeRabbit
                                #   walkthrough, Superagent "found N", Socket table)
  "checks": [                   # the bot's check runs at the current head
    {"name", "status", "conclusion", "title", "summary", "url"}
  ],
  "extra": {...}                # provider specifics, documented per adapter
}
```

Severity vocabularies are per reviewer and stored as the bot names them:
CodeRabbit `critical|major|minor|nitpick`; Superagent `P1|P2|P3`; Greptile
findings carry `severity: None` (its semantic read supplies the class);
Socket has no inline findings.

`extra` per adapter:
- greptile: `{"summary_sha": str|None, "check_title": str|None}`
- coderabbit: `{"actionable": int|None, "premerge": {"passed": int, "failed": int}, "review_id": int}`
- superagent: `{"trust_score": int|None, "trust_verdict": str|None, "concerns": int|None}`
- socket: `{"alerts_status": "success"|"failure"|"neutral"|None, "report_url": str|None}`

**Bars**:
- greptile — `pass` when `score == threshold` (threshold = `TRIAGE_REVIEW_THRESHOLD` else `score_max`) and `reviewed_sha == head`; `stale` when scored at another commit; `fail` otherwise; `pending` when no entry.
- coderabbit — `fail` when any unresolved, non-outdated finding of severity `critical|major` exists; `stale` when the latest review's commit is not the head (and nothing is failing); `pass` when reviewed at head with no such finding; `pending` when no entry.
- superagent — `fail` when any unresolved, non-outdated `P1|P2` finding exists at head, or the `Superagent Security Scan` check concluded `action_required|failure`; `pending` when that check is not completed; `pass` when completed `success` with no such finding. `Supply Chain Scan` neutral and a `Contributor trust` verdict other than `safe` do not fail the bar; they are carried in the digest as warnings.
- socket — `fail` when `Socket Security: Pull Request Alerts` concluded `failure`; `pending` while in progress; `pass` on `success|neutral|skipped`; `na` when no Socket check exists on the head (Socket skips PRs with no dependency change).
- A reviewer that is not active yields `na` whatever the entry holds.

### `pipeline/review_fetch.py` — the GitHub feed (new)

`fetch_feed(n: int, head_sha: str) -> PrFeed` — one GraphQL call per PR
(`reviews(last:50)`, `reviewThreads(first:100)` with each thread's first
comment + `isResolved` + `isOutdated`, `comments(last:100)`, every node with
`author { login __typename }` and `databaseId`), plus the head's check runs
through `gh.check_runs`, which grows to carry `app`, `title`, `summary` and
`html_url` per run (still deduped by name+conclusion, `filter=latest`). `PrFeed`
is a plain dataclass of lists; adapters pull what they recognise by login
substring / app slug. The GraphQL transport is `gh.gh_graphql`.

`pipeline/greptile.py` keeps `parse_confidence_score`, `_strip_html`, the
score/SHA regexes, and `fetch_greptile_feedback` (the retrigger-wait version
token — generalized to `review_fetch.version(reviewer, entry)` below). Its
`fetch_greptile_verdict` / `fetch_greptile_review_data` / `backfill_greptile_data`
are removed; the ingest path and the semantic read use the feed.

### Store

- New PR section **`reviews`**: `{<reviewer id>: <entry>, ...}` plus the usual
  `checked_at` / `against_head_sha` stamp. Written by ingest beside `signals`
  via `Pr.stage_facts(..., reviews=...)` / `Pr.set_reviews`. Validated in
  `store.validate_pr`: known reviewer ids, `kind` from the registry, `findings`
  a list of dicts with string `severity`/`path`, `checks` a list of dicts.
- `signals.greptile` and `signals.greptile_reviewed_sha` are removed from the
  signals builder; `Pr.greptile` / `Pr.greptile_reviewed_sha` read
  `reviews.greptile.score` / `.reviewed_sha`.
- `greptile_review` (the agentic semantic read) stays, shape unchanged; the
  driver selects candidates from `reviews.greptile` and reads the Greptile
  findings/summary from the stored entry instead of fetching.
- New singleton registry **`reviewers`**: `{"seen": {<id>: {"last_observed_at":
  iso, "prs": int}}, "computed_at": iso}` — recomputed at the end of every
  `ingest.refresh_prs` over the open corpus (max `observed_at` per reviewer).
  `Store.reviewers_registry()` / `save_reviewers_registry()`.
- `freshness.SHA_BOUND` gains `"reviews"`.
- `schema.STORE_SCHEMA_VERSION` → **19**: `reviews` section + `reviewers`
  registry; an older reader finds no Greptile score in `signals` and judges
  every PR un-reviewed.
- `pipeline/migrate_reviews.py` (store_edit-style: dry-run default, pre-image
  snapshot, runs-ledger entry) lifts `signals.greptile*` of every row into
  `reviews.greptile` so closed-PR history keeps its score; open PRs are
  rewritten by the next ingest anyway.

### `pipeline/review_policy.py` — which reviewers gate, at what bar

Replaces the single `ReviewPolicy`:

```python
@dataclass(frozen=True)
class ReviewPolicy:
    mode: str                      # "auto" | "none" | "explicit"
    explicit: tuple[str, ...]      # reviewer ids when mode == "explicit"
    active_days: int               # TRIAGE_REVIEWER_ACTIVE_DAYS, default 14
    threshold: int | None          # TRIAGE_REVIEW_THRESHOLD (Greptile score bar)

def active_reviewers(kind: str | None = None) -> list[Reviewer]
def is_active(reviewer_id: str) -> bool
def bar(pr: Pr, reviewer: Reviewer) -> Bar
def clean_blockers(pr: Pr, kind: str) -> list[Blocker]   # Blocker(reviewer, bar)
def merge_bar_sentence() -> str
def describe() -> dict   # the capabilities descriptor
```

`active_reviewers()` = `explicit` list in explicit mode; nothing in `none`
mode; in `auto` mode every registry reviewer whose `reviewers` registry
`last_observed_at` is within `active_days`. The registry read goes through the
store singleton with a per-process cache invalidated by `onboarding.reconfigure`
and by the ingest that rewrites it (same pattern as the caches `reconfigure`
already resets).

`settings.parse_review_provider` accepts `auto` (new default), `none`, or a
comma-separated list of known reviewer ids; anything else is a hard error
listing the known ids. `TRIAGE_REVIEW_THRESHOLD` keeps its meaning (Greptile
score bar). New `TRIAGE_REVIEWER_ACTIVE_DAYS` (default 14).

### Gates (`pipeline/gates.py`)

- `pr_clean` requires `is_current(pr, "reviews")` alongside `signals`; appends
  one reason per `review`-kind blocker (`"greptile 3/5"`, `"awaiting coderabbit
  review"`, `"coderabbit: 2 open major findings"`, `"coderabbit review stale"`)
  and one per `scanner`-kind blocker (`"superagent: 2 open P1 findings"`,
  `"socket: new dependency alerts"`). Reviewers not active contribute nothing.
- `bar_asks` reads `review_policy.clean_blockers(pr, "review")` directly and
  returns each blocker's `ask`; the string-prefix match on the label is gone.
  `merge_demotion` is unchanged in shape: any review or scanner blocker demotes
  a stored `merge` to `request-changes` with those asks.
- `fix_huntable`: the review branch reads blockers of kind `review`; a `fix`
  is huntable when `"review"` is a fixable gate, at least one active reviewer
  is `fail` (not `stale`, not `pending`), and no scanner blocks (a scanner
  finding is a needs-human signal, not a fix target).
- CI derivation (`ingest._ci_from_github_data`) excludes check runs whose app
  slug belongs to any registry reviewer, so a Greptile 4/5 or a Superagent
  `action_required` reads under its own name and CI reflects the repository's
  own workflows. `signals.ci` keeps its three values.

### Ingest (`pipeline/ingest.py`)

`refresh_prs` fetches the feed per PR (replacing the Greptile-only fetch),
parses every registry reviewer (not only active ones — detection needs the
data), stages `reviews` with `signals`, and after the loop recomputes the
`reviewers` registry. The `--backfill-greptile-data` flag is removed; the feed
carries the reviewed SHA. `_build_signals` loses its two Greptile parameters.

### Semantic read (`pipeline/greptile_read_driver.py`)

Candidates: open PRs whose `reviews.greptile` is current, scored below the bar,
and whose `greptile_review` is not current. Batches carry the stored entry's
findings + summary instead of re-fetching. Unchanged prompt, workflow, eval.
The phase stays Greptile-specific on purpose: CodeRabbit and Superagent name
their own severities.

### Agents

- **ANALYZE** bundle (`analyze_driver`): each member carries `reviews: {<id>:
  digest}` (score/bar/stale/open findings by severity/summary line) instead of
  `signals.greptile`; `merge_bar_sentence()` lists every active reviewer's bar
  ("external review: Greptile 5/5, CodeRabbit no open Critical/Major findings;
  scanners clean: Superagent, Socket; CI passing; mergeable"). Prompt prose
  generalized.
- **SECURITY** manifest item gains `bot_evidence`: scanner findings (path,
  line, severity, title, body) and review-kind findings at head; `security.js`
  hands it to the three lenses as prior evidence to confirm or refute, never as
  a verdict.
- **Chat** context lines use `reviewers.summary_line(pr)` ("Greptile 4/5 ⚠stale
  · CodeRabbit 2 major · Superagent P1 · Socket ok"); `{review_bar}` uses
  `merge_bar_sentence()`.
- **Autofix** goal (`fix_worker._fix_goal`) gathers `findings_for_fix` across
  active review-kind reviewers and a review summary from each (Greptile score
  comment, CodeRabbit walkthrough) when the reviewer's bar is `fail`;
  `_hunt_key` tiers on `severity == "nits"` then "one below a scored bar"
  (`score == threshold - 1`), then rest. `_retrigger_review` after a pushed fix
  retriggers every active reviewer with a mention.
- **deep_search / training** read `reviews` digests.

### App backend

- `caps.capabilities()["reviewers"]`: `[{id, label, kind, active, retrigger,
  score_max, threshold, bar_label}]` in registry order; `["review"]` is removed
  (frontend migrates).
- `service.pr_row`: `row.reviews = {<id>: digest}` for every reviewer with an
  entry or active; `row.signals` keeps `greptile`, `greptile_stale`,
  `greptile_severity` (projected from the Greptile digest — they are Greptile
  facts and remain the search vocabulary when Greptile is active).
- `pr_checks`: the `review` row aggregates active review-kind bars (pass when
  all pass; fail when any fails; warn when any stale/pending; na when none
  active), detail naming each; new `scans` row aggregates scanner bars the same
  way. `checkDefs` gains `scans`.
- `filters.py`: new spec keys `review_status: {<id>: pass|fail|stale|pending}`
  and `scan_status: {<id>: ...}`; the three Greptile keys stay.
  `pr_search` vocabulary lists them when the reviewer is active.
- Routes: `GET /api/prs/{n}/reviews` (stored section + bars);
  `POST /api/reviews/{reviewer}/retrigger/pr/{n}` replaces the Greptile route;
  `executor.retrigger_review(n, reviewer_id, ...)` posts that reviewer's
  mention, activity kind `review_retrigger` with `reviewer` in the payload
  (old `greptile_retrigger` entries still read). Bulk action `REVIEW_RETRIGGER`
  with `reviewer`. `review_refresh` polls `review_fetch.version` for the named
  reviewer.
- `freshness_live` emits `{"kind": "review", "reviewer": id, ...}` per active
  reviewer whose `reviewed_sha` is behind the live head.
- `pr_history`: `kind: "bot_review"` with `reviewer` and `score` (Greptile) /
  `actionable` (CodeRabbit) / `concerns` (Superagent).
- `responses._is_human` unchanged (already generic over bots).
- Onboarding `connect` step keeps `TRIAGE_REVIEW_PROVIDER` in its allowlist;
  the wizard offers `auto` (default) / `none`.

### Frontend

- `ExecContext` exposes `reviewers: ReviewerCap[]`; helper `activeReviewers(kind)`.
- PR Explorer: the `greptile` column becomes **`review`** ("Review"): one chip
  per active review-kind reviewer from `row.reviews` (`Greptile 4/5 ⚠`,
  `CodeRabbit 2 major`), hover body listing each reviewer's status line, stale
  note and summary excerpt; new **`scans`** column ("Scans") with one chip per
  scanner (`Superagent P1`, `Socket ok`, trust score in the hover). Both carry
  `capability` gating on having at least one active reviewer of the kind.
  Column prefs key `greptile` is renamed `review` in `useColumnPrefs` with a
  one-time alias read.
- Filter popout for `review`: per-reviewer status select + the existing Greptile
  score/freshness/severity controls when Greptile is active; for `scans`:
  per-scanner status select. Chip labels from `prFilterParts`. Search-bar hint
  lists the active reviewers.
- `lanes.MERGE_READY_SPEC` = `{checks: ALL_CHECKS_PASS, safety: "GREEN"}` (the
  `review` and `scans` check rows now carry the bars); `homeCards` likewise
  drops raw Greptile clauses except the "nitpicks" card, which stays Greptile
  (it is the semantic-read card) and is gated on Greptile being active.
- PR detail: `checksBodies.review` renders one block per active review-kind
  reviewer (status, stale banner, score or finding counts, summary markdown,
  findings list `path:line — severity — title`, retrigger button when the
  reviewer has a mention); `checksBodies.scans` one block per scanner (check
  rows, findings, trust score). `FactFreshness` labels `reviews`; `PRHistory`
  renders `bot_review` with the reviewer label.
- Glossary: `col.review`, `col.scans`, `review.<id>` terms; `bulk.REVIEW_RETRIGGER`.

## Data flow

```
GitHub ──feed (GraphQL + check runs)──▶ ingest.refresh_prs
   └─ reviewers.parse × 4 ──▶ Pr.reviews[id] (stamped)  ──▶ reviewers registry
                                        │
   review_policy.active_reviewers ◀─────┘ (auto: registry window; explicit: env)
                                        │
   gates.pr_clean ◀── clean_blockers(review) + clean_blockers(scanner)
   pr_checks review/scans rows ◀── bars
   analyze / security / chat / fix ◀── digests, findings, evidence
   app rows + PR page + filters ◀── digests
```

## Error handling

- Feed fetch failure for a PR: keep the stored `reviews` (not re-stamped, so it
  reads stale and blocks clean rather than reading "no reviewers").
- An adapter that cannot parse a bot's comment records the entry with what it
  could read (`score: None`, `summary` kept) — the bar then reads `fail` for a
  scored reviewer ("greptile ?/5"), never `pass`.
- Unknown bot logins are ignored; the registry grows by code change only.
- `TRIAGE_REVIEW_PROVIDER` naming an unknown id is a hard error at settings
  parse (same as today's unknown provider).
- A `reviews` section from a newer registry (unknown id) is preserved verbatim
  by `validate_pr` with the existing unknown-key notice, and ignored by bars.

## Testing

- `pipeline/tests/test_reviewers.py`: each adapter against fixture feeds built
  from the real Paperclip payloads captured for this design (Greptile comment +
  failing check; CodeRabbit review + findings + walkthrough; Superagent review
  + P1/P2 threads + three checks; Socket alerts success/failure/neutral); bars
  for every status; `findings_for_fix` excludes scanners and resolved/outdated
  threads.
- `test_review_fetch.py`: GraphQL → `PrFeed` mapping with a stubbed transport.
- `test_review_policy.py`: `auto`/`none`/explicit parsing; activity window;
  `clean_blockers` by kind; descriptor.
- `test_gates.py` additions: multi-reviewer `pr_clean` reasons; scanner block;
  `bar_asks` from blockers; `fix_huntable` with a scanner block; CI excludes
  reviewer check runs.
- `test_ingest.py` / `test_refresh_prs.py`: `reviews` staged beside signals;
  registry recomputed; CI derivation.
- `test_store.py`: `reviews` validation; `test_schema_*`: version 19.
- Backend: `test_caps_reviewers.py`, `test_pr_checks.py` (review/scans rows),
  `test_filters.py` (new keys), `test_service_store.py` (row.reviews), executor
  retrigger per reviewer, `test_review_refresh.py`, `test_freshness_live.py`,
  `test_pr_history.py`, `test_fix_worker.py` (multi-reviewer goal), chat
  context lines.
- Frontend: `homeCards.test.ts` representable keys, `rowReuse.test.ts`
  fixtures; `pnpm run build` + lint gate.
- The two `conftest.py` fixtures that pin `TRIAGE_REVIEW_PROVIDER=greptile`
  keep working (explicit mode → Greptile gates regardless of detection).

## Out of scope

- Dismissing/resolving a bot's findings from Prospector.
- Per-reviewer configurable severity bars beyond Greptile's threshold (the
  CodeRabbit `critical|major` and Superagent `P1|P2` bars are fixed; a later
  profile field can open them).
- Dynamic per-reviewer check rows in the frontend (`review` and `scans` stay
  aggregate rows; per-reviewer detail lives in the column hover, the popout
  filter and the PR page).
