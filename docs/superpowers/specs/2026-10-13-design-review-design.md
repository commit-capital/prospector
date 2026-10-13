# Design review — a Prospector-run quality reviewer

## Why

Greptile is the only quality signal on `TRIAGE_REPO` PRs, and autofix iterates
to its 5/5. Nothing independently checks reuse, layering, drift from the
repository's own conventions, or whether a PR's tests would catch a broken
implementation. Design review is a second, repo-aware reviewer that Prospector
runs itself. It is advisory first — shown, never gating — and gates only once
an operator, having measured its precision, turns that on.

The deterministic companion (changed-line coverage / mutation testing in the
verify sandbox) is out of scope and follows separately.

## The agent — `pipeline/design_review.py`

One locked-down headless run per PR head: `headless_agent.run_agent` with
`allow_gh=False`, `edit_root=None`, `env_allow=()`, `cwd` and
`read_root=[clone, diff]` where `clone` is this machine's pinned default-branch
clone (`verify_driver.base_clone_dir(pin sha)`) and `diff` is the head's cached
diff (`diff_cache.DIFFS/<head>.diff`) — the same two inputs the verify blind
pass reads. No Docker is needed; a machine without a pin clone or without the
diff cached runs nothing.

The prompt asks for four judgments of the diff against the base tree:

1. **reuse** — an existing helper, type, constant or test utility the diff
   duplicates. The agent must grep the base and cite the existing path.
2. **layering** — the change placed outside the module that owns the behavior.
3. **convention** — drift from the repository's contributor docs and
   `rules/*.mdc`. The docs come from `authoring.docs_block(docs_from_tree(clone))`,
   ranked below the prompt's rules; the agent reads `rules/` in the clone itself.
4. **test** — assertions that would survive a broken implementation,
   over-mocking, an edge case the diff introduces that no test exercises.

It answers one JSON object:
`{"summary": str, "findings": [{"category": "reuse|layering|convention|test",
"severity": "blocking|major|minor", "path": str, "line": int|null,
"title": str, "body": str, "evidence_path": str|null}]}`.

The host validates the answer: unknown categories or severities are dropped, a
finding's `path` must be a file the diff touches, and a reuse finding's
`evidence_path` must exist in the base clone — a finding failing either is
dropped. `review()` returns the entry or raises `DesignReviewFailed` when the
agent did not finish or gave no usable answer; `AgentUnavailable`,
`AgentDeclined`, `CapacityExhausted` and `AgentTransient` propagate for the lane
to book. A patch past `review_fix`'s size is clipped the same way.

## Storage

The entry lives at `reviews["design"]`, built with `reviewers._entry`:
`reviewed_sha` = the head judged, `observed_at`, `findings` (each with
`resolved`/`outdated` False so `open_findings` reads them), `summary`,
`extra={"base_sha", "model"}`. It is written only through
`Store.save_design_review(n, entry, head_sha)`, a compare-and-swap on the row
that refuses when the stored head is no longer `head_sha`.

Ingest replaces the whole `reviews` section with `reviewers.parse_all(feed)`, so
`parse_all` carries every Prospector-run reviewer's entry over from `previous`
unchanged. Older code would drop it on ingest, so
`schema.STORE_SCHEMA_VERSION` goes to 31.

## Registry and policy

- `Reviewer` gains `source: str` (`"github"` default, `"prospector"`).
  `DESIGN = Reviewer("design", "Design review", REVIEW, (), (), None, None,
  source="prospector")`. `by_login`/`by_app` never match it (empty tuples).
- `_design_bar`: no entry → `pending`; `reviewed_sha` ≠ head → `stale`; any open
  `blocking` finding → `fail` ("design review: N open blocking findings");
  otherwise `pass`.
- `review_policy.active_reviewers`: auto mode never detects a `prospector`
  reviewer. DESIGN is active (and so gating) only under
  `TRIAGE_DESIGN_REVIEW=gate`, or when `TRIAGE_REVIEW_PROVIDER` lists `design`
  explicitly.
- `reviewers.digests` shows DESIGN whenever it has an entry; when it is not
  active the digest carries the reviewer's own bar and `advisory: True`, so the
  PR page's Checks card shows it tagged advisory. `bar_label(DESIGN)` = "Design
  review with no open blocking findings".
- `seen_summary` ignores `prospector` reviewers.

## The lane

`design` joins the verify worker's idle hunt (`verify_worker.next_auto`):

- **Pool** (`design_review.huntable(pr)` in pipeline, pure): open; threat
  verdict at the current head is `clear` (`gates`' threat-current check);
  not `gates.bump_exempt`; the head's diff is cached; no DESIGN entry at the
  current head; no live `design_run` claim elsewhere; not in the lane's
  in-memory skip set (6-hour expiry, as `security_failed`).
- **Order:** maintainer PRs first, then merge picks, then by pain, then lowest
  number — the existing `_key`.
- **Rotation:** the hunt's alternation becomes a round-robin over the lanes
  whose pools have work (`security`, `verify`, `design`), maintainer priority
  first as today, so design reviews progress while the security backlog drains.
- **Run:** `Store.claim_design_run(n, host, head_sha, stale_after)` /
  `release_design_run` on a top-level `design_run` field — one claim per head,
  shaped like `claim_security_run`. The run happens in-process under
  `capacity.metered("design")` after `lane_health.capacity_open("design")`.
- **Health:** `design` joins `worker_health.LANES`. A finished review books
  success; `DesignReviewFailed` books a machine failure and parks the PR; an
  agent outage trips every agent lane; a decline parks the PR without a failure.
- **Ledger:** every ending appends a `design-review:run` row with the PR, head,
  finding counts or the failure reason.
- **Setting:** `TRIAGE_DESIGN_REVIEW` = `off | advisory | gate`, default
  `advisory`, editable on the Setup page. `off` skips the lane; `advisory` runs
  it without gating; `gate` runs it and makes its bar a merge requirement.

## Autofix

`design` joins `profile.AUTOFIX_GATES`. When the profile names it:

- `fix_worker._fix_goal` adds DESIGN's open blocking/major findings (via
  `reviewers.findings_for_fix`) when its own bar fails at the current head,
  whether or not it gates, with the goal line "Fix the design review findings
  listed below."
- `gates.fix_huntable` counts a failing DESIGN bar as a fixable gate for `fix`.

Nothing else about who autofix may touch changes; `fix_eligibility` and the
re-gate are unchanged.

## Testing

Unit tests for: the bar's four states; auto mode never activating DESIGN and
`gate` activating it; `parse_all` carrying the entry through ingest; the claim
CAS (held elsewhere, own leftover, stale reclaim, head moved); answer
validation (bad category, path outside the diff, reuse evidence missing from
base); `huntable`; the hunt rotation; `_fix_goal` and `fix_huntable` with and
without `design` in `fixable_gates`; the advisory digest. Then pyright 0, ruff
clean, frontend build, full pytest.
