# Reviewer eval ratchet

## Why

Prospector's own reviewers decide what lands upstream and what a contributor
is sent back for: design review, security review, issue-fix review, trust
boundary, intake audit and fixability. Nothing measures whether they are right.
A prompt edit or a `TRIAGE_AGENT_MODEL` change can make one worse and nobody
would know. The Claude Code team's answer
(claude.dev/blog/solving-code-review-at-anthropic) is a ratchet: past outcomes
become eval cases, the reviewer is scored against them, and a prompt, tool or
model change is kept only if the score holds.

We have no labeled examples yet. So the first job is to start capturing every
verdict with enough to replay it, and to turn the outcomes we already record
into labels. The gate comes last and stays provisional until there are cases
to gate on.

## Goals

- Every run of the six reviewers leaves a replayable record: inputs, output,
  prompt identity, model, the head or base it judged.
- Outcomes become labels, from people (a per-verdict "was this right?") and from
  sources whose meaning is reliable without a person.
- A scorecard per reviewer: how its production verdicts fared against labels,
  by prompt and model, over time.
- A prompt or model-default change to a reviewer cannot merge without an eval
  run showing it is no worse than the pinned configuration.

## Non-goals

- Tuning third-party reviewers (Greptile and other bots). We only read them;
  `greptile_read` stays as is.
- Gating an operator's runtime model choice. `TRIAGE_AGENT_MODEL` in a
  deployment's `.env` is theirs; the scorecard shows its effect, the gate covers
  the code's defaults.
- Calling a model in CI.
- Pre-existing-finding tagging, diff-only re-review, revert detection. They feed
  this later and are separate changes.

## Anti-goals

- Labels the system invents and then trusts. A derived signal only *suggests*
  a label; a person confirms it, except for the auto sources named below.
- A capture that grows with history unbounded, or a queue that reads the whole
  store per request.
- A gate so noisy it gets bypassed. One agent run per case is noisy; the gate
  must not flip on a single coin toss.

## Concepts

- **Reviewer**: one of `design`, `security`, `fix-review`, `trust-boundary`,
  `intake`, `fixability`. Each has a normalized **verdict** (below), and the
  first three also emit **findings**.
- **The reviewer is the whole chain** that produces the verdict as production
  stores it: every agent prompt in it (design review *and* design verify;
  security's lenses *and* verifier) and the deterministic adjustments
  (design's rubric bar, intake's blocklist and hidden-text overrides, trust
  boundary's host scan). The verdict recorded is the one after those
  adjustments. A caller's policy exemption is not the reviewer: trust
  boundary's verdict is `hold(…, maintainer_filed=False)`, whoever filed.
- **Prompt identity** (`prompt_sha`): a hash of every prompt template constant
  in the chain as written in source (before per-run fills such as `__REPO__`),
  plus a hand-bumped logic version per reviewer for the deterministic part
  (design already has `DESIGN_RUBRIC`). One registry names both per reviewer.
- **Model**: the sorted list of models the chain ran, stored as one string
  (fix-review's cross lane runs several; the rest run one). The pin compares
  against the list the code defaults produce.
- **Configuration**: `(prompt_sha, model)`.
- **Case**: a captured verdict with a label.
- **Label**: the verdict the reviewer should have given, optional marks on its
  findings (`real | wrong | intentional | didnt-matter`, the Claude Code
  team's four), who or what set it, and a note.
- **Pin**: the configuration of each reviewer that last passed the gate, with
  its score and case count, checked into the repo.

Normalized verdicts, so one scorer covers all six:

| Reviewer | Verdict | "Blocks" means |
|---|---|---|
| design | `pass` < `fail` from `reviewers._design_bar` | `fail` |
| security | `GREEN` < `YELLOW` < `RED` | `RED` |
| fix-review | `safe` < `unsafe` (any lens) | `unsafe` |
| trust-boundary | `pass` < `hold` from `trust_boundary.hold` | `hold` |
| intake | `clear` < `suspicious` < `malicious` | not `clear` |
| fixability | `go` < `ask` < `skip` | `skip` |

Verdicts are ordered by severity. A label records the expected verdict in the
reviewer's own vocabulary. A verdict *less* severe than its label is an
**under-call** (a RED labeled case judged YELLOW is one); a verdict *more*
severe is an **over-call**. The scorecard and the gate count these, so the
middle values need no special rule.

## Data (signed off 2026-10-23: one new table, schema bump)

One append-only table, `review_verdicts`, one row per reviewer run:

- hot columns: `id`, `at`, `reviewer`, `subject_kind` (`pr | issue`),
  `subject`, `head_sha` / `base_sha`, `prompt_sha`, `model`, `verdict`
- `data` JSON: the reviewer's output as persisted today (findings etc.), plus
  references to inputs already stored elsewhere (a PR diff by head SHA in
  `diffs`, which is immutable)
- `inputs_gz`: gzip of the filled inputs that are *not* stored anywhere else —
  issue report text as judged, the fix patch the reviewer saw, the evidence
  string, security's bot evidence. This is what makes issue-fix and
  trust-boundary verdicts replayable once their worktree is gone.
- `label` JSON, nullable, the only mutable field, written by one store helper.

Retention: `inputs_gz` is cleared after 30 days unless the row is labeled or a
signal (below) currently points at it. Metadata rows stay; they are small.
Retention lands with labeling (step 2): clearing inputs before anything can
label them would throw away the first month of cases.

This is history, not per-record state, so it is a table like `runs` and
`activity`, not a record section. The current section on each record (e.g.
`pr.security`) stays the live verdict; this table is what the section
overwrites today.

Live-scale numbers (rows per day across the six phases in the runs ledger,
compressed input size) are for the builder to measure against
`TRIAGE_STORE_URL` before coding; see Constraints.

## Capture

Each reviewer's own entry point records its run, because only it holds the
exact filled inputs (issue-fix review's evidence string, for one, is built
by the caller and never saved):

- design: `design_review.review`
- security: `security_review.review_pr`, including the flagged findings the
  verifier refuted, which are dropped today
- fix-review: `review_issue_fix.full_review`
- trust-boundary: `trust_boundary.judge`
- intake: `intake_audit.judge`
- fixability: `fixability.judge`

Each takes an explicit `subject`. Production callers pass it, and an eval call
without one records nothing, so red-team runs never pollute the table.

`INCOMPLETE` / failed runs are recorded with that verdict so the scorecard can
show failure rates, and are never cases.

## Labels

**Auto sources** (no person needed, because the outcome defines the answer):

1. *Known-clean*: a PR a maintainer merged upstream, not through us, that was
   not reverted within 30 days. Design and security should not block it. Until
   revert detection exists, "not reverted" is checked by hand at labeling time
   or the source stays off.
2. *Red-team rows*: `issue_redteam.jsonl` cases already carry `expect`; they
   enter as intake and trust-boundary cases directly.

**Proposed, needs a person**:

3. *Replay oracle*: `issue_fix_replay`'s `false_accept` / `false_reject` for
   fix-review. Proposed only, because an oracle fail is often a contract
   mismatch, not a wrong fix. Replay runs write their own `review_verdicts`
   rows with `subject_kind = replay`; the oracle result proposes a label on
   that row. The scorecard reports production and replay apart.
4. *Signals* derived on read from facts already persisted, over the last 30
   days of verdicts:

| Reviewer | Signal | Suggests |
|---|---|---|
| security | operator merged over YELLOW (`security_override` on the merge activity row) | too strict |
| security | RED on a PR maintainers merged upstream | false block |
| design | blocking finding, PR merged anyway | false block |
| design | finding's line changed by the contributor's next head | real |
| fix-review | all `safe`, proposal closed by maintainer (`fix_followup.closed_as = closed`) or CI `caused` (`ci_attribution`) | miss |
| trust-boundary | `hold`, then operator proposed anyway | false hold |
| intake | not `clear`, operator proceeded | false flag |
| fixability | `skip`, then `fix_scan.fixed_by` | false skip |
| any | reviewers passed a change, then an operator sent it back with guidance (PR `fix` request, issue `send-back` / `solve`) | under-call; the guidance is the label's note |
| fix-review | reviews `safe` (or scope-safety coverage all `addressed`), and another author's PR on the issue is merged, or `second_opinion` names cases our fix misses | under-call: our fix was worse than the rival |

The PR-side guidance is overwritten with the next request today, so the
`fix:single` ledger row carries `guidance` from step 1 on. Issue guidance is
already kept in `fix_thread`.

Chat transcripts (`chat_messages`, keyed `pr-N` / `issue-N`) are not mined for
labels. The labeling UI shows the subject's chat from around the verdict so the
operator can read it.

**Human labeling** happens where the verdict is shown (PR page, issue fix
review) and in a queue of signalled, unlabeled verdicts. One click confirms the
suggestion; otherwise the operator picks the expected verdict and marks
findings. Placement and layout are the builder's.

## Case studies

An operator's written case study of one attempt (issue #14867's is the
first) is the richest label we get. It attaches to the subject's verdict rows,
and each lesson in it is classified by what it is about:

- **reviewer**: becomes the label and note on that reviewer's row. On #14867
  both fix reviews said `safe` while flagging "this part of the fix is
  incomplete", so the row is labeled an under-call.
- **fixer**: the agent that wrote the fix (a reproduction asserting a log note,
  a capability cut to remove a crossing, plumbing tested only by reading it).
  The issue becomes a fixer replay case: `issue_fix_replay` run against the
  better PR's tests as the oracle. Scoring the fixer is the next ratchet, after
  this one; until then these cases accumulate.
- **gate**: a policy defect, tracked as an issue against its owner
  (`gates.py`, `issue_gates.py`), as commit-capital/prospector#657 is. It
  is not a label.

Attempts that ran before capture (#14867 ran 2026-10-22) get a one-time
backfill row from the issue's `fix_run` and its `result.json` where that still
exists. The evidence string is lost for those, so the row is marked
`partial_inputs` and replays without it.

## Scorecard

Derived on read from labeled rows only, per reviewer and configuration:

- must-catch recall: of cases labeled as blocking, how many it blocked
- under-call and over-call rates across all cases
- false-block rate: of cases labeled non-blocking, how many it blocked
- finding precision, for reviewers with findings
- failure rate, from all rows

Shown on the Policy page beside the trust ladder, which is the same idea per
action type (`prospector_app/backend/trust_ladder.py`). With fewer than 10
labeled cases on a side, the number shows as "too few cases", not a rate.

## The gate

`pipeline/evals/data/reviewer_pins.json` holds each reviewer's pinned
configuration, score and case count. A test without model calls fails when a
reviewer's current `prompt_sha` or code-default model differs from its pin.

To change one, run `python -m pipeline.evals.reviewer_eval --reviewer X --live`.
It replays every case for that reviewer under the pinned and the candidate
configuration (rendering inputs to a case dir as `prompt_behavior` does,
checking the base out by SHA for clone-reading reviewers), scores both, and
rewrites the pin only if the candidate:

1. under-calls no case the pinned configuration called right, and
2. has an over-call rate no worse than the pin's, and
3. any case where the two disagree was re-run until the disagreement holds
   (builder sets k; 3 is a reasonable start).

The pin records the commit that set it. Results per `(case, configuration)`
are kept as `reviewer-eval:case` rows in the runs ledger and reused, so the
pinned side mostly comes from earlier runs. A case with no pinned-side result
(labeled since the pin) runs the pinned code from a temporary checkout of the
pin's commit. Each eval also writes a `reviewer-eval:run` row, so a pin traces
to the run that set it.

Cases stay in the store and are never exported to the repo: they hold
outsiders' text, including attacks. So the eval runs on an operator machine
with store and model access, as the issue-fix replay does, and a prompt PR's
author (or an operator) runs it and commits the re-pinned file.

**Provisional mode**: until a reviewer has 10 must-catch and 10 should-pass
cases, the eval still runs and re-pins, but rules 1–2 report instead of
refuse. The pin file marks the reviewer `provisional`. This builds the habit
before the data exists.

## Order of work

1. Table, schema bump, `verdict_log`, `prompt_sha` registry, capture at the six
   sites, `guidance` on the `fix:single` row. Start now: every day without it
   is unrecoverable cases.
2. Auto sources and the label helper with the per-verdict control.
3. Signals queue and scorecard.
4. Pins, the CI test, the eval runner, provisional mode.

Each is its own PR.

## Constraints

- AGENTS.md invariants: store access through `pipeline/store.py`, rows
  validated on write, schema version bumped, every new env variable registered.
  The eval runner never writes upstream and uses a disposable store for agent
  bookkeeping, as `issue_redteam.provider()` does.
- Architecture review: no new worker sweep (signals are computed on read over a
  bounded window, a fixed number of queries, not per row) and no cache. Measure
  the queue and scorecard reads against the live store and put the numbers in
  the PR.
- Captured inputs contain outsiders' text. They are data to agents in the
  eval, under the same headless-agent lockdown as production.

## Left to the builder

- Module names beyond `verdict_log` and `reviewer_eval`, and where the
  `prompt_sha` registry lives (it must be importable by both production and
  evals without a cycle).
- Exact `inputs_gz` contents per reviewer, as long as a case replays from the
  row plus immutable store data plus a base checkout.
- How retention clears expired inputs (on insert, or in an existing GC).
- UI placement and layout.
- Case sampling if a reviewer's case set gets expensive to replay.

## Calls made without the operator

- Six reviewers in scope, captured together; labels and gate per reviewer.
- 30-day windows for retention and signals; 10/10 cases before the gate
  enforces.
- A pin file checked into the repo, enforced by a model-free test, rather than
  CI calling a model.
- Signals computed on read, not by a new sweep.
- The reviewer is the whole chain (verifiers and deterministic overrides
  included); `model` is the sorted list of models the chain ran.
- Verdicts are ordered by severity and scored as under-calls and over-calls.
- Replay runs write their own verdict rows, kept apart from production.
- The pinned side replays from cached per-case results, falling back to a
  checkout of the pin's commit.
