# Never present stale pipeline facts as current

## Problem

The pipeline detects staleness correctly and then throws the detection away.

Every sha-bound fact section is stamped `against_head_sha`, and
`freshness.is_current()` compares that stamp to the PR's head. But the head it
compares against is `meta.head_sha` — the head the *last ingest* saw. The app's
live sweep (`freshness_live.sweep`) fetches the real upstream head, notices it
moved, hands the React client an ephemeral `diverged[]` warning, and never
writes the observation down:

- `record_live_state` persists `state`, `ci`, `mergeable`, `diffstat`,
  `has_tests` — not the head.
- `persist_live` skips the store write entirely when the head is the *only*
  thing that changed (all other args are `None`, so the loop `continue`s).
- `ingest.meta_from_gh` is the only writer of `meta.head_sha`.

So between an author's push and the next ingest, every reader that consults
freshness — the gates, `STATUS.md`, briefings, the chat agent, Explorer rows,
cluster views — reports a stale analysis as current, with no hedge. Only a
client sitting on that PR's detail page when `/freshness` fires learns
otherwise, and that knowledge dies in React state.

Two smaller gaps compound it:

- **The write path gates on head for merges only.** `check_head=True` appears at
  exactly one call site (`merge_pr`). `execute_pr` (close), `submit_review`,
  `comment_line`, and `retrigger_greptile` all pass `check_head=False`, so
  posting a request-changes comment confirms the PR is open and not that the
  head still matches the analysis it quotes.
- **`stale_sections` is computed, shipped, and rendered nowhere.**
  `service.py` puts it in the payload, `api.ts` types it, no component reads it.
  No view shows `checked_at` either, so "when was this analysis from?" has no
  answer in the UI.

## Design

### 1. Effective head — the root fix

Persist the observed upstream head as its own fact and derive freshness from it.

- `meta.live_head_sha` — the head GitHub reported at the last live observation.
  Written by `record_live_state`; an *observed upstream fact*, exactly like
  `meta.state` and `signals.ci` that the sweep already persists.
- `Pr.effective_head_sha` = `meta.live_head_sha or meta.head_sha`.
- `freshness.is_current()` / `currency_failure()` token against
  `effective_head_sha` instead of `head_sha`.

Consequences, all from one change at the root of the ONE freshness module:

- Every existing `is_current` / `stale_sections` / `currency_failure` consumer
  starts telling the truth with no per-call-site plumbing, including
  `gates.merge_allowed` — a stale analysis stops auto-recommending merge.
- `head_sha` keeps meaning "the head we have an ingested diff and signals for",
  so the diff cache, verify, and security stay coherent. Nothing keyed on
  `head_sha` changes behaviour.
- Nothing derived is stored; staleness stays derived on read.
  `ingest.meta_from_gh` rebuilds `meta` wholesale, so a re-ingest drops
  `live_head_sha` and the read heals in place.

`currency_failure` distinguishes the two stale shapes, because the operator
response differs:

- `against_head_sha` matches neither head → `"stale (computed against an
  earlier head)"` — re-run the phase.
- `against_head_sha` matches `head_sha` but not `live_head_sha` →
  `"stale (new commits upstream, not yet ingested)"` — re-ingest, then re-run.

`freshness.upstream_head_moved(pr) -> bool` names the second condition for
callers that need it (the write gate, the UI banner).

### 2. Persist the observation

- `record_live_state` gains `live_head_sha: str | None`.
- `persist_live` computes a `live_head_arg` and includes it in the
  "anything to write?" check, so a head-only push is no longer skipped.
- Storing a `live_head_sha` equal to `head_sha` is harmless and self-clearing:
  `effective_head_sha` collapses to the same value.

### 3. Greptile as its own divergence axis

`signals.greptile_reviewed_sha != head` means the review verdict is behind the
code even when our own analysis is current — the live state on #9368. Since the
review provider's bar is a hard merge requirement, this earns a `diverged[]`
entry of `kind: "greptile"`, distinct from `kind: "head"` because the remedy is
a review re-trigger, not a re-analysis.

### 4. Write gate: block by default, override recorded

`_preflight` returns a `Preflight` NamedTuple — `ok: bool`, `message: str`,
`kind: str | None` where kind is one of `merged | state | head | conflicts |
unconfirmed` — so a caller can tell a staleness block from an
already-merged block. All six call sites are updated.

The four evidence-quoting writes pass `check_head=True`:

| call site | why it quotes evidence |
|---|---|
| `execute_pr` (close) | the close comment states a disposition derived from the diff |
| `submit_review` | request-changes quotes the analysis rationale |
| `comment_line` | anchored to a specific file and line of the diff |
| `retrigger_greptile` | re-review is head-relative |

A staleness block returns `status: "stale"` (not `"skipped"`) plus a `stale`
payload naming what moved — old sha, new sha, the stale section names and their
`checked_at` dates — so the UI can render a specific confirm rather than a
generic error. The action bodies gain `override_stale: bool = False`; passing it
proceeds and stamps `stale_override=True` on the Activity entry.

Non-staleness blocks (merged, closed, conflicts) keep refusing as they do today
— the override covers stale evidence only, never a PR that is gone.

Live-read failure stays fail-open for these writes (`fail_closed=True` remains
merge-only): a transient GitHub read outage should not wedge commenting.

### 5. Read surface

- Render `stale_sections` with each section's `checked_at` on PR detail, so the
  answer to "when is this from?" is on screen next to the rationale.
- Show the disposition's own provenance inline: the sha it was computed against
  and the date.
- The Greptile divergence flows into the existing `FreshnessCallout`
  automatically once it is in `diverged[]`.
- A `status: "stale"` action response opens the override confirm.

### 6. Store schema version

Bump `schema.STORE_SCHEMA_VERSION` 13 → 14. A checkout without this change
ignores `live_head_sha` and reads stale facts as current — today's bug rather
than corruption, but squarely the "older code mishandles the record" case the
constant exists to guard.

## Testing

- `freshness`: effective-head tokening; the two distinct `currency_failure`
  phrasings; `upstream_head_moved`; re-ingest clearing `live_head_sha` heals the
  read.
- `gates`: `merge_allowed` goes false on an un-ingested upstream push.
- `persist_live`: a head-only push writes (the regression that motivated this);
  `record_live_state` round-trips `live_head_sha`.
- `freshness_live`: the `greptile` divergence axis fires on sha mismatch and
  stays quiet when Greptile is current.
- `executor`: each of the four sites blocks on a moved head with
  `status: "stale"`; `override_stale=True` proceeds and logs the override;
  a merged-upstream PR still refuses regardless of `override_stale`.

## Out of scope

- Ingest scheduling and sweep cadence. This design makes lag *visible* rather
  than eliminating it.
- `meta.checked_at` age as an independent staleness axis. Once the sweep
  persists the head, a stale `live_head_sha` observation is the thing that
  matters, and it is covered above.
