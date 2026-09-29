# Issue-fix review in the app

The issue-fix factory (`issue_triage/cross_lane.py`, `propose.py`,
`dispute_question.py`) runs from the command line on the worker machine and keeps
its results in files there. This design brings it into the app: an operator
finds issues by fix status in the Issues explorer, reads a fix attempt in the
issue's detail, and acts on it (try to fix, send back with comments, answer a
question, ask the reporter, open the PR), from any machine.

## Decisions (from the design conversation, 2026-09-29)

- **Who answers a dispute first:** the operator, in the app. They pick an
  option or write their own answer; asking the reporter on GitHub is an explicit
  action.
- **How fixes start:** an operator's "Try to fix" on an issue (with optional
  guidance), plus an optional hunter that picks candidates itself within a daily
  budget, off by default. Nothing the hunter produces opens a PR without the
  operator.
- **Sending a fix back:** one action with comments. The worker reads the
  comments: a revision of the current fix by one agent, or a fresh
  three-agent run with the comments as guidance when they ask to start over.
  Either result faces the host's checks and the scope reviewer again.
- **Where it lives:** inside the Issues explorer, not a new tab: a fix-status
  filter and column, the fix in the issue detail, Home cards that link to the
  filtered explorer.
- **Architecture:** a store-backed queue mirroring autofix. The app writes
  requests; a worker lane on the machine that holds the sandbox drains them and
  writes results back; the app reads only the store.

## Store (schema 26)

Three issue sections:

- `fix_request` — the one pending action: `action` (`solve`, `send-back`,
  `answer`, `ask-reporter`, `propose`), `status` (`queued`, `running`, `done`,
  `failed`, `cancelled`), `source` (`operator`, `hunter`), `requested_by`,
  `queued_at`, `host`, `started_at`, `finished_at`, `guidance`, `answer`,
  `dry_run`, `reason`. A claim is a compare-and-swap on the record's `saved_at`.
- `fix_run` — the latest attempt, distilled for display: the lane, the ending
  and its reason, the base and report shas, the host that holds its files, each
  candidate's summary, root cause, tests, per-file rationale or reason for giving
  up, the agreement matrix and readings, the picked diff (bounded), the check
  results, the reviewers' verdicts, concerns and unasked changes, the question
  and whether it was asked, and the proposal. Agent transcripts stay out.
- `fix_thread` — the conversation, oldest first: operator comments and answers,
  the worker's notes. Bounded to the newest entries.

Every attempt still appends its `issue-fix:run` ledger row, so history outlives
the section the next attempt overwrites.

## Worker lane (`TRIAGE_ISSUE_FIX_WORKER=1`, lane `issue-fix`)

One request at a time, the oldest queued first:

- `solve` — the cross-tested lane with the request's guidance. A dispute drafts
  its question at once, so the operator can read it.
- `send-back` — a small agent reads the comments: `revise` runs one agent on a
  clone that already holds the current fix, with the comments and the reviewers'
  concerns, then the host's checks and the scope review; `restart` runs `solve`
  with the comments as guidance.
- `answer` — an option resumes the disputed run on its reading; free text
  re-solves with the question and answer as guidance.
- `ask-reporter` and `propose` — the existing bot paths
  (`executor.ask_issue_question`, `executor.propose_issue_fix`), dry-run when the
  request says so, logged in Activity.

A run's follow-ups need its files and its held base, so `send-back`, `answer`,
`ask-reporter` and `propose` are claimed only by the host `fix_run` names. The
lane also reads the replies to questions asked on GitHub and resumes on an
answer or, after a week, on the default. The hunter (`TRIAGE_ISSUE_FIX_HUNT=1`,
`TRIAGE_ISSUE_FIX_HUNT_BUDGET` per day, default 5) queues `solve` for recent,
well-reproduced issues with no linked PR and no attempt, when the queue is
empty.

## Status (derived on read, never stored)

`fix_status`: `running` (queued or running), `review` (a fix waiting for the
operator), `question` (a dispute waiting for the operator), `reporter` (a
question asked on GitHub), `pr-open`, `declined` (the run's reason), `failed`
(a machine fault, retryable), or none. The Issues query filters and sorts on
it, and Home counts `review` and `question`.

## API

- `GET /api/issues/{n}` carries `fix_status`, `fix_request`, `fix_run`,
  `fix_thread`.
- `POST /api/issues/{n}/fix` `{action, guidance?, answer_label?, answer_text?,
  dry_run?}` queues a request (refused while one is in flight, or when the
  action does not fit the run's state) and records the operator's words in the
  thread.
- `POST /api/issues/{n}/fix/cancel` cancels a queued request.

## UI

The Issues explorer gains a fix-status filter and column. The issue detail gains
an Auto-fix section: the status and reason, the candidates side by side, the
agreement between them, the picked diff (`DiffView`), the checks, the reviewer's
verdict and concerns, the question with its options, the thread, and the
actions that fit the status (Try to fix, Send back, Answer, Ask the reporter,
Open PR — dry-run aware). Home gains two issue cards.
