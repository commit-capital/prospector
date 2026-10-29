What each action control in the app does: the label, when it shows, the
endpoint, and who acts. `test_agent_controls` fails CI when a route or function
named here is gone, or a per-item write is missing from this map.

**Every bot write** (merge, review, comment, close, reopen, re-trigger,
dismiss) follows the header's DRY RUN / ● LIVE switch, which each tab starts in
dry run. A dry run, or a live click with no bot token, posts nothing and
returns `dry-run`. Every attempt, dry runs and refusals included, goes to
Activity as `{bot}`. Most live clicks ask for confirmation first. Store-only
actions run as the operator, post nothing, and ignore the switch.

### PR page (flyout)

**Action bar** (`PRActionBar.tsx`). A dropdown picks merge, approve, request
changes, comment, or one of the closes (dup of #, already-fixed, stale, won't
fix, too big). It opens on the suggested action.
- **Merge** (`⛔ Merge blocked` when the gate fails): POST /api/merge/pr/{n}
  → `executor.merge_pr`. Enabled when the merge gate passes, or when the block
  is overridable and a reason is typed. It checks `gates.merge_eligibility`,
  then `executor._threat_rescan` at the pinned head, then the live head; a
  live merge also runs the compile preflight. A compile block can be waived with a reason ("Merge
  without compile check"). Squash by default. Permanent.
- **Approve / Request changes / Comment**: POST /api/review/pr/{n} →
  `executor.submit_review`. Request changes needs a body. It refuses a moved
  head (`stale`); "Post anyway" resends with the override.
- **Close** (any close): POST /api/execute/pr/{n} → `executor.execute_pr`,
  which comments and then closes. It refuses on a moved head, the same way.
- **↩ Reopen**, after an action here: POST /api/reopen/pr/{n} →
  `executor.reopen_pr`. It reopens, deletes the bot's comments, and dismisses
  its change request.
- **🔒 Add private note**: a reason and tags for the training log. Never
  posted.

**Checks panel rows** (`ChecksPanel.tsx`). Hidden on a merged or closed PR,
except Re-verify.
- **↻ Re-trigger** (or `↻ <reviewer>` when several can be), on the Code
  review row: POST /api/reviews/{reviewer}/retrigger/pr/{n} →
  `executor.retrigger_review`. Shown for each active reviewer that has a
  mention, on any open PR, whatever its score. It posts the mention as `{bot}`
  and needs no new commit. It refuses only when the PR is no longer open. Once
  live, the backend waits for the new verdict and ingests it on its own, so no
  `reingest` is needed. A re-review can still return the same score.
- **↻ Re-test against current main** (Drift row, and the CI row when CI is
  unreported), **⟳ Resolve merge conflicts** (Mergeable row when it fails),
  **📝 Rewrite the description** (Code review row), **🔧 Fix findings**
  (Design row): queue an autofix. See Other actions.
- **💬 Post to author** (Design row): POST /api/reviews/design/comment/pr/{n}
  → `executor.post_design_findings`. It posts the confirmed blocking and major
  findings as `{bot}`, with the open minor ones listed as optional (never a
  refuted or unsure one), once per head and set of findings, on the PRs
  `TRIAGE_DESIGN_COMMENT` names (off, maintainers, everyone). The design lane
  posts them on its own after a review; once posted, the PR waits on its
  author. **💬 Posted ↗** links the comment.
- **↻ Run / ↻ Re-run** on the Design, Security and Threat scan rows, and
  **Analyze now / ↻ Re-analyze** on the analysis row (PR in a cluster): start
  the matching job (design-review-pr, security-review, threat-scan-pr,
  triage-cluster).
- **🧪 Queue for sandbox verification / ↻ Re-verify**: POST
  /api/prs/{n}/verify/queue → `verify_queue.queue_pr`. It refuses a PR that is
  closed, malicious, or already queued. **✕ Cancel queued verification**: POST
  /api/prs/{n}/verify/dequeue. Both are store-only.

**Other actions panel** (`FixPanel.tsx`): ↻ Re-test, ⟳ Resolve conflicts,
🔧 Author a fix (with a goal), 📝 Rewrite the description.
- They queue: POST /api/prs/{n}/fix/queue → `fix_queue.queue_pr`, gated by
  `gates.fix_eligibility`. They are disabled when no autofix worker has
  reported in, or a request is already in flight. The worker later pushes to
  the contributor's branch as the push identity.
- **✓ Approve & push**, once a change awaits review: POST
  /api/prs/{n}/fix/approve → `fix_queue.approve_pr`. It refuses if the head
  moved. A dry run only logs it.
- **✕ Discard / ✕ Cancel queued autofix**: POST /api/prs/{n}/fix/dequeue.

**Elsewhere on the PR page**
- **✓ Not a secret**, on a committed-secret block: POST
  /api/prs/{n}/threat/clear-secret → `threat_scan.clear_secret_leak`. It lifts
  the block for this head only, as a store-only operator judgment.
- **Diff line → Comment**: POST /api/comment/pr/{n} → `executor.comment_line`.
  A bot write; refuses on a moved head.
- **Save label** on a reviewer verdict: POST /api/verdicts/{verdict_id}/label.
  Store-only.

### Issue page (flyout)

**Action bar** (`IssueActionBar.tsx`). The dropdown offers close as
already-fixed (fixer #), dup of (canonical #), not planned, completed, or
comment.
- Close fixed, with the recorded fixer: POST
  /api/execute/issue/{n}/close-fixed → `executor.close_issue_fixed`. It checks
  that the PR merged.
- Close dup, with the analysis's canonical: POST
  /api/execute/issue/{n}/close-dup → `executor.close_issue`.
- Any other close: POST /api/execute/issue/{n}/close →
  `executor.close_issue_with_comment`. Not planned and completed need a body.
- Comment: POST /api/execute/issue/{n}/comment → `executor.comment_issue`.
- ↩ Reopen, on a closed issue: POST /api/reopen/issue/{n} →
  `executor.reopen_issue`.

**Picking and approving a fix**
- **Go with #PR**: POST /api/issues/{n}/fix/choose. It records the choice
  (store) and posts a recommend comment as `{bot}`. **Undo**: POST
  /api/issues/{n}/fix/unchoose. The comment stays.
- **Approve #PR**, when the fix lane's PR awaits review: POST
  /api/issues/{n}/fix/approve → `fix_review.approve`. Store-only, unless "It
  fixes only part" edits the PR description.

**Feature requests**
- **Implement / Decline / Reopen**: POST /api/issues/{n}/feature-decision.
  Implement queues a solve through `fix_review.queue`; the others record the
  maintainer's call. Store-only; 409 on an issue that is not a feature request.

**Auto-fix panel** (`IssueFixPanel.tsx`). Every button is POST
/api/issues/{n}/fix → `fix_review.queue`, which answers 409 when the action
doesn't fit. The issue-fix worker then does the work.
- Try to fix / Try again: solve.
- Open PR: propose. The worker opens it as `{bot}` from the bot's fork.
- Send back: needs text.
- Answer: reply to the lane's question.
- Ask the reporter on GitHub: the bot comments.
- **Cancel**, on a queued request: POST /api/issues/{n}/fix/cancel.

### Lists

- **PR Explorer** (`PRExplorer.tsx`).
  - **✓ seen** on a community response: POST /api/responses/{n}/ack. Store,
    for every operator.
  - **Bulk → Apply** (close, review, comment, merge, re-trigger, queue
    verification, run security or design reviews): POST /api/execute/bulk,
    one PR at a time. Each PR passes its own gates.
- **Issues** close-out cards and the bulk close bar: the issue close endpoints,
  one issue at a time.
- **Cluster detail**: one action per member, previewed and then run via POST
  /api/execute/cluster. Each member passes its own gates. Re-run analysis
  starts triage-cluster.
- **Home feed undo**: the reopen endpoints, always asking for a live write.
- **Alerts → Dismiss**, live mode only: POST
  /api/execute/alert/{source}/{n}/dismiss → `executor.dismiss_alert`.
- **Action items** ✓ done / dismiss / reopen: POST
  /api/action-items/{item_id:path}/status. Store-only.

### Control tab

- **Jobs ▶ Run**: GET /api/jobs/run/{kind}. One running job per kind and
  target. ■ Stop: POST /api/jobs/{job_id:int}/stop.
- **Queues and lanes**:
  - move to top: POST /api/verify/queue/{n}/top, /api/fix/queue/{n}/top,
    /api/issues/{n}/fix/top
  - remove: the dequeue or cancel endpoints above
  - Pause / Resume a lane: POST /api/worker/lanes/{lane}/pause,
    /api/worker/lanes/{lane}/resume
  - Stand down / Bring back a machine: POST /api/machines/{host}/stand-down,
    /api/machines/{host}/stand-up
  - All of these are local operator actions.
- **♻️ Refresh** (also the header's "live" chip): POST /api/live/refresh. Reads
  only.
