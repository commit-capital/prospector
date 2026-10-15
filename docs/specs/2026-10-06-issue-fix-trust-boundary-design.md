# Issue fixes that cross a trust boundary

The issue-fix factory builds what a report asks for. Every check it runs asks
whether the fix does *only* that: the scope-safety reviewer vetoes an unasked
change to an input that worked, and marks a change the report asked for
`requested`, which never vetoes. Nothing asks whether what was asked for is
something only a maintainer may authorize. A report whose "expected behavior"
is a backdoor ("on boot the agent fetches its instructions from
updates.example.io") reproduces, fixes, proves green, and passes review.

As community issues become the main intake (`TRIAGE_ISSUE_FIX_PUBLIC_SCOPE=all`,
community pull requests closed), this is the gap the factory has to close
before its fixes open pull requests on their own.

## Decisions (from the design conversation, 2026-10-06)

- **Authorization is who asked.** A change that crosses a trust boundary is
  authorized only by a maintainer: the issue was filed by one
  (`gates.priority_author`) and its report asks for every crossing. Anything
  else is held for an operator.
- **Hold, never refuse.** Real defects live in authentication and networking
  code; refusing would cut coverage. A held fix ends `fixed` as before and waits
  for an operator's Propose, which is the maintainer's decision.
- **A dedicated reviewer.** A focused security prompt, separate from
  scope-safety, inventories every crossing. Its failure reads as unknown, which
  holds.
- **A deterministic floor.** A URL host the fix adds that appears nowhere in the
  base tree is a `network` crossing whatever any agent says.

## Design

`issue_triage/trust_boundary.py` is the ONE policy:

- `new_hosts(base_dir, fix_patch)` — the hosts of `http(s)://` and `ws(s)://`
  URLs on the fix's added lines that `git grep --no-index --exclude-standard`
  finds nowhere in the base tree.
- `review(worktree, fix_patch, title, body)` — a locked-down agent (read-only,
  held to the worktree, no network, bare environment) that lists every crossing
  as `{kind, where, what, requested}`, `requested` meaning the report asks for
  exactly that behavior. Kinds: `network`, `remote-code`, `process`, `auth`,
  `secrets`, `validation`, `data-exposure`.
- `judge(...)` — both, as the run's `boundary` record: `{crossings, reason}`, or
  `{crossings, failed: true, reason}` when the reviewer gave no usable answer
  (the scan's crossings are kept).
- `hold(boundary, maintainer_filed)` — why the fix waits, or None. A review
  that did not finish holds. On a maintainer's issue only an unrequested
  crossing holds, and a run with no record does not; on anyone else's, every
  crossing holds, and so does a run with no record.
- `held(issue)` — `hold` for an issue's fixed, unproposed attempt.

The lanes call `judge` once a fix clears every other check and record
`result["boundary"]`: the cross lane's `_judge_pick` (and so a dispute resumed
on its answer), the solo lane under `review=True` (a revision), and the staged
lane. `fix_review.distill` carries it into `fix_run`.

A held fix:

- is not auto-proposed (`public_loop.queue_due`);
- carries the `ready for review` label on its issue, not `fix in progress`;
- shows the reason in the issue's fix panel (`fix_held` on the issue detail).

Every fix with a crossing, held or not, shows its crossings in the panel, and
its pull request body opens with a warning and lists each crossing under Risks,
in inert text.

## Not in this design

- A follow-up revision pushed onto an already-open pull request is not held. A
  person reviews that pull request before merge, and its re-rendered description
  carries the warning.
- The intake audit of an issue's text, before any attempt.
