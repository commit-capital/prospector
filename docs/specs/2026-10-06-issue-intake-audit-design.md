# Auditing an issue before the factory builds it

The trust-boundary review (`2026-10-06-issue-fix-trust-boundary-design.md`)
judges the code a fix wrote. This audit judges the text before any fix is
written, so the factory spends nothing building what an outsider's report
smuggles in, and so a maintainer sees what the agents were asked.

Two gaps close here. Every issue-fix agent reads the report through
`reproduce_issue.report_block`, which passed the raw body on, HTML comments
GitHub never renders included: an instruction a maintainer cannot see on
GitHub reached every agent. And nothing judged the request itself before the
lanes ran.

## Decisions (from the design conversation, 2026-10-06)

- **Agents read what GitHub shows.** `report_block` hands every issue-fix agent
  the visible text: HTML comments outside code fences and invisible characters
  (zero-width spaces, bidi controls, Unicode tag characters, the variation
  selectors that smuggle bytes) are removed.
- **Only outsiders' issues are audited.** A maintainer's issue is taken as
  written.
- **A malicious verdict stops unattended work only.** The hunter's and the
  public loop's solves end `refused`; an operator's own solve runs, since the
  operator chose it.
- **Anything short of clear holds the fix.** A suspicious report, or a malicious
  one an operator ran, holds its fix through `trust_boundary.held`.
- **Deferred:** blocking a malicious issue's author on the actor blocklist (an
  agent's false positive would also block their pull requests), a per-author
  cap on the hunter, and a failing-test field in the issue template.

## Design

`issue_triage/intake_audit.py` is the ONE policy for issue text as input:

- `visible(text)` — the text as GitHub renders it.
- `hidden(text)` — what `visible` removes, described: each HTML comment and
  each class of invisible character.
- `review(title, body, notes)` — a locked-down agent with no tools reads the
  raw report and the reporter's notes as data and returns `clear`,
  `suspicious` or `malicious` with findings `{kind, quote, why}`: `instruction`
  (text addressed to an AI or automation), `boundary` (a request to cross one
  of the trust boundaries), `deception` (false authority, impersonating a
  maintainer, urgency). A refusal by the model's safeguards is a finding; any
  other failure is `failed`.
- `judge(title, body, notes, blocked)` — the run's `intake` record. An author on
  the threat blocklist reads `malicious` with no agent run; hidden content and a
  failed review read at least `suspicious`.
- `refusal(intake)` — why an unattended attempt does not run; `flag(intake)` —
  why its fix waits for an operator.

`fix_review_runner.solve` judges an outsider's issue on the pinned base before
the cross lane. A refused attempt records the ending `refused` with its
`intake` and runs no lane; otherwise the lane runs and its result carries
`intake`. `fix_review.distill` copies it into `fix_run`.

A `refused` attempt reads `declined` in the app, carries no public label and
no comment, and is not restarted by replies or edits; the hunter passes it by.
The issue's fix panel shows the audit's verdict and every finding with its
quote.
