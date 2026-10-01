# Issue fixes on GitHub: the public loop

The issue-fix factory (`issue_triage/cross_lane.py`, `fix_review.py`,
`fix_review_runner.py`, `followup.py`) is driven from the Prospector app: an
operator opens each pull request, posts each question, and reads every
outcome there. This design lets a maintainer of the triaged repository use the
factory from GitHub alone. The maintainer files an issue, watches a label move,
and gets a question, a "couldn't fix" with the reasons, or a pull request that
iterates until it is green. The maintainer replies or reviews to steer it, and
merges it when it is right.

## Decisions (from the design conversation, 2026-10-01)

- **Scope:** issues whose author `gates.priority_author` names (a GitHub
  maintainer association — OWNER, MEMBER, COLLABORATOR — or a profile
  `priority_authors` login) get the public loop. Every other issue behaves as
  it does today: attempted by the hunter, visible in the app only.
  `TRIAGE_ISSUE_FIX_PUBLIC_SCOPE=all` widens it to every attempted issue.
- **No click gates in scope:** the hunter picks issues on its own (it already
  does); a fixed result opens its pull request and a drafted question is posted
  without an operator. Nothing merges on its own.
- **The store stays the state machine.** GitHub gets output only: one status
  label, conclusion comments, questions, pull requests. The worker never reads a
  label back. Its GitHub inputs are replies on the issue from its author or a
  maintainer, and maintainer reviews on the pull request — commands, read the
  way the letter answer to a question is read today.
- **Replies:** a small agent reads the new comments and decides whether they
  carry something actionable; only those start a re-attempt, at most three per
  issue.
- **Architecture:** one sync pass on the issue-fix worker's idle ticks derives
  what GitHub should show from the store, compares it with a record of what it
  has posted, and makes only the missing writes.

## What GitHub shows

### Labels

At most one of five labels, lowercase like the repository's own, swapped by the
sync pass whenever the derived state changes:

| Store state | Label |
|---|---|
| a request queued or running; a fixed result waiting for its auto-propose; a drafted question waiting for its auto-ask | `fix in progress` |
| a question posted on the issue, unanswered | `needs answer` |
| a pull request open, the follow-up watching it | `iterating on PR` |
| the follow-up ready, or handed back | `ready for review` |
| the attempt ended without a fix (or the re-attempt cap is reached) | `couldn't fix` |
| a machine fault, the issue closed, the pull request merged or closed, no attempt | none |

While a pull request is open, it carries the same label as its issue. Labels go
through the issues label endpoints (which serve pull requests too); `gh pr edit`
stays off the executor path. The sync pass creates the five labels on the
repository when they are missing.

### Comments

Each attempt gets at most one conclusion comment, rendered by the host from a
fixed template. Agent-written fragments (summary, root cause) are clipped and
held to inert plain text; every comment passes the same `problems` gate the
dispute question does (no link, no live @mention, no closing keyword, no HTML,
no bidi control) and carries a hidden marker naming the issue, attempt and kind,
so a post is never repeated.

| Ending | Comment |
|---|---|
| `fixed`, once its pull request opens | on the issue: the pull request number, and that the follow-up iterates on it until CI and code review pass |
| `fix-disputed` | the existing lettered question (`dispute_question.render`), posted automatically |
| `not-reproduced`, `wrong-symptom`, `unwritable` | couldn't reproduce: what the reproduction tried, and a request for steps, version, or the error |
| `not-a-defect` | read as intended behavior, with the reason, and an invitation to reply with the expected behavior |
| `no-fix`, `fix-untrusted`, `fix-unproven`, `fix-rejected`, `declined`, a question no agent could draft | reproduced (when it was) but no fix passed every check: the root-cause notes and where it fell short |
| the re-attempt cap reached | that the bot leaves it for a maintainer, once |

On the pull request, the follow-up posts once per head: ready (CI and the
active reviewers pass) or handed back (what is unresolved).

Machine faults post nothing. A cancel for an edited report re-runs on its own
(below); a cancel for a closed issue posts nothing. A conclusion comment is
posted only for an attempt that finished within the last day, so an attempt
that concluded before the loop existed gets its label and no late comment.

## What the worker reads back

### Replies on the issue

For an in-scope issue whose label is `needs answer` or `couldn't fix`, the sync
pass reads the comments after the newest one it has handled, kept when the
author is the issue's author or a maintainer (`gates.priority_author` on the
comment's author association) and is not the bot.

- On a posted question, a reply whose first line names an option is the answer
  (`dispute_question.parse_answer`), as today.
- Anything else goes to a small router agent (no tools, the comments as quoted
  data) that returns `retry` (new reproduction detail, the intended behavior, a
  pointer) or `none` (thanks, a mention, chatter).
- `retry` on a question queues `answer` with the comments as its written
  answer (the runner re-solves with the question and the answer as guidance);
  on a `couldn't fix` it queues `solve` with the comments as guidance.
- An edit to the issue's title or body after a concluded attempt (a changed
  report sha) is new information: it queues `solve` the same way.
- Each re-attempt counts; after three, the cap comment posts once and replies
  start nothing more. An operator in the app can still act.
- The newest comment handled is recorded, so a comment is routed once.

### Reviews on the pull request

`followup.read` also returns maintainer feedback: reviews by a maintainer
(changes requested, or a comment with a body or inline comments) and
conversation comments by a maintainer, newer than the feedback it last acted on,
never the bot's or the push user's. The same router decides `retry` (asks for a
change) or `none` (approval, LGTM, chatter).

`decide` takes acted-on maintainer feedback before every bot signal: after
`done` and the older-pull-request hand-back, feedback is a `revise` whose
guidance quotes it, with its own budget of three maintainer revisions. New
feedback also releases a ready or handed-back hold, which otherwise holds until
the head moves. The revision goes onto the same branch through the existing
follow-up revision path.

## Plumbing

- `issue_triage/public_loop.py` — the ONE policy for the public loop:
  `in_scope`, `label_for` (pure, from `fix_review.fix_status` plus the run and
  follow-up), the comment due for an attempt, `sync` (labels, comments,
  auto-queue, replies) and `refresh` (fresh issues).
- `issue_triage/public_comments.py` — rendering and `problems` for the
  conclusion and pull-request status comments.
- Store, schema 28: issue section `fix_public` — the labels set per number, the
  comments posted per key (`<attempt>:<kind>`, with url), the requests it
  queued, a short per-issue lease so two workers never write for one issue at
  once, the last write error (retried after thirty minutes), and the dry-run
  record kept apart. The re-attempt count and the newest comment handled join it
  with the replies. Fix requests gain the source
  `public`, which an older validator refuses.
- Executor: `set_fix_label` and `post_fix_comment` through `_bot_write`,
  Activity kinds `issue-fix-label` and `issue-fix-comment`. A pass writes for an
  issue only under its lease, taken by compare-and-swap, and a comment is
  checked against the bot's comments for its marker before it is posted.
- Safety guard: `label_bot_run`, holding a label write to the five names, on
  `repos/<repo>/issues/<n>/labels` (add, remove) and `repos/<repo>/labels`
  (create).
- Auto-queue: a fixed attempt with no pull request queues `propose`, a drafted
  question not yet asked queues `ask-reporter`, both with source `public`; the
  host whose files hold the run claims them (`issue_fix_worker.next_request`).
- Hunter: an issue a maintainer filed skips the reproduction-grade filter (a
  maintainer's terse report is still a report); the labels it skips, its age
  limit and the linked-PR check stand.
- Fresh issues: `refresh` reads the issues updated since its last pass (a day
  back after a restart) through the REST issues endpoint, keeps the in-scope
  ones, and ingests them through `issue_ingest.ingest_records`, so a new
  maintainer issue reaches the store within one pass without a full ingest.
- The worker runs `refresh` then `sync` every ten minutes on an idle tick.
- Settings: `TRIAGE_ISSUE_FIX_PUBLIC` — `live` (default), `dry-run` (each
  write is noted on the issue's thread in the app and nothing is written
  upstream), `off`; `TRIAGE_ISSUE_FIX_PUBLIC_SCOPE` — `maintainers` (default)
  or `all`.

## Errors

- A failed write is logged to Activity and retried on a later pass, no sooner
  than thirty minutes after the failure for that issue.
- A rendering that fails `problems` is noted on the thread and not posted; the
  label still moves.
- A router agent that answers nothing usable reads as `none` for that pass and
  the comments stay unhandled, so the next pass routes them again; an agent
  outage trips the lane as every agent outage does.
- Sync never trips the lane on a GitHub failure: those are upstream, not the
  machine.

## Testing

- `label_for` and the comment due, over every status, ending, follow-up state
  and scope.
- Rendering and `problems` for each comment kind.
- `sync` against a fake executor: labels swapped once, comments posted once,
  a second pass a no-op, dry-run writes nothing, a lost claim posts nothing,
  out-of-scope issues untouched.
- Reply routing with a fake router: letter answers, retry, none, the cap, the
  edit trigger, the seen marker.
- `followup.decide` with maintainer feedback: precedence, budget, hold release.
- The safety guard: label writes outside the five names, or to any other
  endpoint, refuse.

## Delivery

Two pull requests: the first carries everything GitHub shows (labels,
comments, auto-propose and auto-ask, the sync pass, the executor and guard
paths, the light refresh, the hunter's in-scope grade rule); the second carries
what the worker reads back (issue replies and maintainer reviews).
