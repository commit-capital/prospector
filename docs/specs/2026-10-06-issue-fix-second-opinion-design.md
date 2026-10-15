# Another author's pull request as a second opinion

The factory stepped aside whenever someone else's pull request named an issue:
the attempt was marked `superseded`, the hunter skipped issues with a linked
pull request, the propose step refused while one was open, and the follow-up
handed our pull request back when an older one named the issue. The project is
moving to community issues and agent-written fixes, where a community pull
request is a statement of what a good fix should do, not code to take — taking
it would reintroduce the vetting this direction removes.

## Decisions (from the design conversation, 2026-10-06)

- **Never stop for a rival.** The four deference points go; `superseded` marks
  stored on existing attempts are ignored.
- **Use its tests, never its change.** A rival's test files run in the sandbox
  against our fix. A test proves something only when it fails on our base and
  passes with the rival's own change; one that then fails with our fix is a gap.
- **One revision per gap set.** Our agent is told what fails and shown the
  tests as data; it writes its own test and change. The revision faces every
  check a fix faces and replaces the fix only when it ends `fixed`.
- **Credit only when it changed the fix.** A closed gap makes the rival's author
  a co-author of the proposal's commit and names their pull request in its
  body; a rival that merely existed earns nothing.
- **Rivals that open after our pull request** get the same judgment from the
  follow-up: each is judged once, a gap the revision closes goes onto our pull
  request with the credit on its commit, and a gap no revision closes hands it
  back.
- **A comparison judge** reads the report, our change and the rival's change,
  with no tools and all three as data, and names in words each case the report
  asks for that their change handles and ours misses. Those cases are gaps too;
  the revision is told the words, never their code, and a recheck compares
  again. A rival without tests is judged by its change alone.

## Design

`issue_triage/second_opinion.py` is the ONE policy:

- `rivals(issue, exclude, registry)` — open pull requests by others whose body
  claims to fix the issue (`related_prs.search`), oldest first, at most
  MAX_RIVALS with tests among MAX_EXAMINED; each passed over (blocklisted author,
  unreadable diff, malicious threat scan, no tests) is a `skipped` entry.
- `judge(base, rival, patch)` — red on the base twice, green with its own change
  twice, then with our patch: `covered`, `gap` (with our run's output), or
  `skipped` with the reason.
- `notes(gaps)` — the revision's notes: the failing test files, our output, and
  the tests quoted as data.
- `recheck(...)` — a gap over the revised fix: `gap-closed` or `gap-open`.
- `credit(entries)`, `co_author(login)` — the authors to credit and their
  Co-authored-by value at GitHub's noreply address.
- `flag(entries)` — why a fix with a gap open waits for an operator; read by
  `trust_boundary.held`.

`followup.poll` names the rivals it has not seen (`fix_followup.rivals_seen`)
and queues a `send-back` carrying them; `fix_review_runner._late_rivals` judges
them against the open pull request's fix, revises through `_revise_proposal`
(whose `amend` rechecks before the push), and hands the pull request back on a
gap left open.

`fix_review_runner.solve` runs it after a fixed cross lane (`_second_opinion`),
records every entry on the run as `second_opinion`, and `fix_review.distill`
carries it to the panel's Second opinion section. `executor.propose_issue_fix`
adds the co-author trailers and the body's credit lines.
