# Feature request screening

## Why

Paperclip's maintainers asked that auto-fix build only defects: users are poor
judges of features. Prospector honors that. Fixability calls a request
`kind: feature` and skips it, and the hunter never triages issues labeled
`enhancement` at all (`issue_fix_worker.HUNT_SKIP_LABELS`). The cost is that
feature requests are the largest group no one acts on (158 at last count), and
a good one is lost in the pile.

This lane lets a maintainer find the few good feature requests without reading
all of them, see what building one would look like, and send it to the fix lane
with one click. An agent screens; a maintainer decides. The agent's opinion
only puts a request on a shortlist and never starts work.

## Goals

- Every open feature request gets a screening verdict judged against what
  Paperclip is and how its maintainers have treated feature requests before.
- A Home card, "Feature requests worth considering", lists only the requests
  the agent likes. Each shows a short reason and a UI mockup.
- "Implement this feature" queues the issue in the issue-fix lane, framed as a
  feature, and it runs through the same proof, review and propose steps a
  defect does.
- "Not for Paperclip" removes it from the card, and that decision teaches the
  screener.

## Non-goals

- Running Paperclip itself (booting the app, a browser, real screenshots). A
  later, separate project.
- Posting anything upstream on screening, approval or rejection. Prospector
  stays silent on the issue until a PR exists, and that PR goes through the
  existing propose step.
- Auto-implementing any feature. Nothing enters the fix lane without a
  maintainer's click.

## Anti-goals

- An agent that likes everything. A card with 100 items is the black hole
  again. The screener should say yes rarely, with reasons a maintainer
  recognizes as Paperclip's.
- Treating the request's text as instructions. Requests are outsiders' text,
  so the screener and the mockup are data handled under the existing
  locked-down agent rules.
- A mockup that can run script or reach the network in the operator's
  browser.

## Design

### Which issues are screened

An issue is a feature request when its current fixability verdict has
`kind: feature`, or it carries the `enhancement` label. The second source
matters because the hunter skips that label before fixability ever runs.
Screening covers open issues of any age. The 30-day hunt window doesn't apply,
because old good ideas are the point.

### The screener

A new module, `issue_triage/feature_screen.py`, holds one read-only headless
agent, shaped like `fixability.judge`: `allow_gh=False`, the base clone and a
history directory as its read roots, no environment. It reads:

- the request and its replies (as data, with the same trust section as
  fixability);
- Paperclip's README, docs and code in the base clone;
- the **taste digest** (below).

It returns JSON:

```json
{"verdict": "consider|not-a-fit|unclear",
 "why": "<two to four sentences: what it adds and why it fits Paperclip, citing docs or past decisions>",
 "precedents": [<issue numbers of past decisions it leaned on>],
 "size": "small|medium|large",
 "ui": true|false}
```

The host keeps only precedents that exist in the digest. A `consider` with no
`why` reads `unclear`.

For `consider` with `ui: true`, a second step produces the mockup (below). The
lane spends mockup work only on the yes pile.

The result is stored as a new issue section, `feature_screen` (add it to
`issue_store.ISSUE_SECTIONS`), and stamped against the issue's content, the
same way `fix_triage` is (`issue_freshness.UPDATED_BOUND`). An edit or a new
reply makes it stale, and the next pass re-screens it. Each verdict also goes
to `verdict_log`.

### The taste digest

The closest record of the maintainers' taste is what they did with past
feature requests. A new read-only fetch pulls closed issues labeled
`enhancement` or previously screened, with `stateReason` (completed or not
planned), labels, linked merged PRs, and maintainers' comment bodies. It is
written to the history read root as plain files, one per issue, regenerated
when stale. Ingest today fetches open issues only and keeps no comment bodies,
so this fetch is new.

The digest also includes this deployment's own "Implement" and "Not for
Paperclip" decisions, read from the store, so the screener learns from the
card too.

### The mockup

The mockup agent reads Paperclip's UI components and styles in the base clone
and writes one self-contained HTML file showing the proposed UI in Paperclip's
look, with the new parts highlighted. It is a faithful sketch, not a
screenshot.

- It is stored in the `feature_screen` section, so every machine's app can show
  it, with a size cap. The host refuses anything over the cap, and anything
  that isn't one HTML document with inline styles.
- The app shows it in an `<iframe sandbox srcdoc>` with no `allow-scripts` and
  a CSP that forbids every network fetch. This is the first agent-authored HTML
  the app renders, so it needs security-focused tests: no script runs, and no
  request leaves the frame.

### Where it runs

As a step in the issue-fix worker, beside `triage()`, under its own capacity
lane (`feature-screen`, registered in `lane_health` and `control_overview`) and
metered with `capacity.metered`. A Control job, `feature-screen`, runs it on
demand, and a CLI backfill (`uv run prospector feature-screen-backfill
[--limit N]`) handles the existing pile. Each run is booked in the runs ledger.

### The Home card

A new card in `views/Home.tsx`, modeled on `HomeAutoFeedCard`: its own list
endpoint returns open issues whose current `feature_screen` verdict is
`consider` and that have no decision yet. Each row shows the title, the `why`,
the size, and a mockup thumbnail that opens the full mockup. It also has two
buttons:

- **Implement this feature** calls the existing `POST /api/issues/{n}/fix` with
  `{action: "solve"}` and a feature flag (below).
- **Not for Paperclip** records the rejection.

Both decisions are logged with `activity.record_issue_fix` (`FIX_FEATURE_GO`,
`FIX_FEATURE_DECLINE`) and stored on the issue as a fact (who, when, which), so
the card, the digest and the issue page all read one record. Neither one writes
upstream.

A decision is its own issue section, `feature_decision`, not part of
`feature_screen`, so a re-screen never overwrites it (Claude's call):

- A decline is permanent until a maintainer reverses it. A re-screen that
  turns `consider` does not bring it back to the card.
- An approved request leaves the card. A failed fix run shows wherever
  failed issue-fix runs show today. A maintainer can re-queue it from the
  issue page, still in feature mode.

The card lists `consider` requests ranked by size (small first), then newest,
with a count. During backfill, the CLI reports the yes-rate, so a screener
that likes too much gets caught before the card is trusted (Claude's call).

### Scope

The lane is general Prospector, not Paperclip-only. The repository's name in
prompts comes from `TRIAGE_REPO`. The labels that mark a feature request come
from the profile, defaulting to `enhancement`. A setting switches the lane on
(Claude's call). "Not for Paperclip" in this document means "Not for
<repo>" in the UI.

The issue page also shows the screening verdict and mockup, so a maintainer can
read the requests the agent didn't like. It carries the same two buttons on
every feature request, whatever the verdict: a maintainer can implement one the
agent didn't recommend. Feature mode follows the maintainer's decision, not the
verdict. With no mockup, the spec is the issue and the maintainer's guidance.
The card stays the agent's shortlist.

### Feature mode in the fix lane

Today the fix prompts assume a defect: `solo_lane`'s author prompt ("You are
fixing a reported defect", give-up kind `not-a-defect`), and
`reproduce_issue`/the judge's `is_defect` in the staged lane. A
`fix_review.queue` request needs a `feature` flag carried through `cross_lane`
to the author, so that in feature mode:

- the first step writes a test pinning the requested new behavior, which fails
  on the base and passes with the change;
- `not-a-defect` is not a reason to give up or block;
- the author's spec is the issue plus the screener's `why` and the mockup.

The author prompt is not the only stage that assumes a defect, so feature mode
reaches every stage that judges the work (Claude's call):

- **Reproduction judge** (`judge_repro`, `issue_gates.reproduction_outcome`): in
  feature mode it rates whether the failing test pins the *approved* behavior,
  in place of `is_defect`. The host still requires red on base and green with
  the change.
- **Scope and safety reviewers** (`review_issue_fix.py`): scope is measured
  against the approved spec (issue, screener's `why`, mockup), because the
  maintainer approved that spec. Changing behavior that worked before is
  expected; the reviewer flags only changes the spec doesn't call for.
  Coverage lists the spec's parts in place of `reported_defects`.
- `claim_test.py`, `trust_boundary.py` and the reviewer preambles get
  feature-mode wording.

The proof bar, the two refuting reviews, the propose gate, and the human
approval before any PR are unchanged.

## Constraints

- AGENTS.md trust model: a read-only screener with no `gh` and no environment;
  outsiders' text is data; the host decides from what it observed.
- The store is the only source of truth. The section holds facts (the verdict,
  the decision), and the card is derived on read.
- Register every new setting in `settings_registry`.

## Left to the builder

- Exact prompt wording, the mockup size cap, and the digest's freshness rule.
- How many requests a worker pass screens.
- The card's visual details.
- Whether feature mode touches the staged `fix_lane` path, or only `cross_lane`,
  which is what `solve` runs.
