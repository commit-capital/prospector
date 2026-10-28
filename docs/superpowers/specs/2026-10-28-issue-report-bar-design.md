# The issue report bar

## Why

The operator's rule (2026-10-28): Prospector judges an issue against what it
concretely asks for. A report that asks for several separate things, or that
falls short in ways only its author can fix, goes back to the author and is
never queued for an automated fix until it meets the bar. An issue that asks
for five things today makes the fix reviewer list five reported defects, and
`issue_gates.fix_coverage` calls a fix that does one of them `partial`, which
pushes the factory toward the five-in-one PR the operator does not want.

This is PR 1 of two. PR 2 (its own session, after this lands) closes
not-in-repo and already-fixed issues unattended. PR 1 only shows them.

## Goals

- The fixability triage (`issue_triage/fixability.py`) is the one judge of the
  bar. It already reads the report, the replies and the code before any fix,
  posts its question through `public_loop.ask_triage`, and goes stale on an
  author's edit or reply (`issue_freshness`), so the "awaiting author" loop
  exists. It gains these checks:
  1. **One concrete ask.** Symptoms, logs or bullets that trace to one root
     cause are one ask. Asks are separate only when the agent can say why: other
     code, another root cause, or one could ship without the other. A report
     with several asks reads `ask`.
  2. **Author gaps**, each recorded as `{kind, ask}` in a new `gaps` list:
     `several-asks` (names each ask and asks the author to file them as
     separate issues), `no-repro` (missing steps, inputs or version), and
     `unsettled` (more than one reasonable correct behavior; today's `ask`).
     Any gap makes the verdict `ask`. The host composes the comment from the
     gaps, lettered A/B/C, in one message.
  3. **Too big.** A fix that cannot be one focused PR reads `skip` with kind
     `too-big`. A report that is big only because it bundles asks is
     `several-asks`, not `too-big`.
  4. **Not sensible.** A request whose change the code deliberately rejects, or
     that would break intended behavior elsewhere, stays `skip`/`intended`.
     Nothing is posted; the operator decides.
  5. **Cleaner fix.** When the report proposes a fix and the code points to a
     better one, the triage records it in `approach`. It blocks nothing; it
     rides to the fix agent in `notes` when the hunter queues the issue.
  6. **External, reworded.** `external` means the cause lives outside this
     repository (a hosted service, a third-party binary, host or OS
     configuration, another product's settings). A defect in this code that
     only shows against an outside service is `go`, proven by a test that stubs
     the service. The live store has 11 `external` skips; reading them, about
     two are the second kind.
- **Who acts on a shortfall:** author gaps go to the author as one comment;
  `too-big` and `intended` wait for the operator; `feature`,
  `already-fixed`, `not-in-repo`, `external` are set aside with their reason
  (PR 2 closes the last three).
- **The hunter** already queues only `go` (`issue_fix_worker.fill`), so
  nothing below the bar is queued. No change there beyond passing `approach`.
- **Issues page:** a derived disposition `request-changes` while the issue's
  current triage is `ask`. It shows in the disposition filter, and the detail
  shows the gaps, whether and when the comment was posted (`asked`), and
  whether the author has replied since. It is computed on read in
  `issue_model.Issue.disposition`, the same way `close-fixed` is, so a fresh
  triage heals it in place. It outranks `request-repro`, `needs-human` and
  unanalyzed; `close-fixed`, `close-dup` and `link-pr` outrank it.

## Non-goals

- Prospector never edits an author's issue text and never files issues for
  them.
- No unattended close (PR 2).
- No change to feature requests (another session).
- No change to `fix_coverage`: once only single-ask reports are `go`, the
  reviewer's reported defects are that one ask.
- Analysis `request-repro` stays as it is; merging it into `request-changes`
  is a later call.

## Anti-goals

- Flagging an ordinary bulleted report as several asks. The prompt makes the
  agent name what separates the asks, and the tests cover a one-root-cause
  report with several symptoms.
- Telling an outsider publicly that their request is a bad idea.
- A second "concrete ask" notion. `several-asks` uses the same sense as the
  claim test's `outside-report` ruling (`claim_test`): what the report asks
  this fix for.

## Constraints

- Store facts, not conclusions: `gaps`, `approach` and the new kind ride in
  the existing `fix_triage` section. Older code reads them harmlessly (it keys
  on `verdict`, which keeps its values, and maps an unknown kind on review
  only), so no `STORE_SCHEMA_VERSION` bump. `request-changes` is derived, never
  stored as an analysis verdict, but the issues table's mirror column carries
  it, so `ISSUE_DISPOSITIONS` validation is unaffected (it checks the stored
  `analysis` section).
- `fixability` is a pinned reviewer: the prompt change needs a re-pin. The
  operator decides whether a provisional re-pin without the hours-long live
  eval is acceptable.
- The comment goes through `public_comments.problems` and `ask_triage`'s cap
  and public-comment setting, unchanged.

## The operator's answers

- **Re-judge old `go` triages.** A triage records the bar it was judged under
  (`bar`); `fixability.current` reads a `go` without the current bar as
  stale, so the hunter re-judges those issues at its usual pace and capacity.
  Older `ask` and `skip` triages stay current, so no author is asked twice
  because the prompt changed. No bulk store edit.
- **Re-pin `fixability` provisionally**, without the live eval.

## Calls made in review (the designer's, not the operator's)

- **After the author answers.** `request-changes` holds only while the triage
  is current, so an issue the hunter cannot re-judge never sticks there. The
  issue's detail still shows the stale ask as "the reporter replied or edited
  it; judging again". The hunter re-triages it on its next pass, as today.
- **After a split.** When the author's reply or edit says an ask moved to
  another issue, that ask no longer counts against this report (the claim
  test's sense of work "left to another issue or pull request"), so the
  original passes on its remaining ask. An author who never answers stays at
  `request-changes` once `MAX_ASKS` is spent; the operator can still act.
- **`gaps` and `question`/`readings`.** The agent returns `gaps`; an
  `unsettled` gap carries its readings, lettered under that gap. The host
  composes `question` from the gaps so older readers still see one.
- **Analysis `asks`** are the missing pieces of a `request-repro` (what to
  supply), not separate requests, so they stay as they are.

## Left to the builder

- Exact prompt wording, within the checks above.
- The comment's wording, within the existing `triage_question` style.
- How the Issues page lays out the gaps (existing flyout patterns).
- Tests: the host's handling of `gaps`/`approach`/`too-big` in `review`, the
  comment rendering, the derived disposition and its precedence, and the
  hunter passing `approach` as notes.
