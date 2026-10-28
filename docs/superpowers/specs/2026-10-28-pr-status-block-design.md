# PR status block — design

## Why

Brandon opens a PR page to answer one question: why isn't this PR fully green and in
"Ready to merge", and will it get there on its own? Today the page can't answer it.

- The "Suggested action" comes from the PR's analysis. On one of our own issue-fix
  PRs (author `commitperclip[bot]`) it can say "comment on the PR". Nobody needs to
  comment on our own PR: the system should be doing that work itself.
- A PR the automation gave up on looks like a list of checks with one red row.
  #15363, #17534 and #17585 were handed back after their revisions, and the only
  sign was a note on the issue's fix thread.
- Nothing says whether the next step happens on its own, or when.

The backend already decides, for every open PR, whose move it is and why
(`prospector_app/backend/automation.classify`: a column of act / auto / handed, a
bucket, an owner of you / worker / author, and a one-line reason). The PR Explorer's
Status column shows that reason. The PR page never does. The issue page solved the
same problem with one plain sentence (`fix_review.status_line`, #692) and an Issue
Explorer column (#693).

## Goals

1. **A status block at the top of every PR page, for every open PR.** It is always
   in one of five states (Brandon's decision):
   - **Ready:** fully green, and merge is the next step (the `merge-ready` bucket).
     It says so, and offers the merge.
   - **Needs you:** one quick click from you, with nothing broken. Approving CI for
     a first-time contributor, re-running stale CI, approving a parked autofix
     change, or closing a PR flagged malicious. It names the click and offers it.
   - **Moving:** a worker or hunter will act on its own. It names the next stage,
     the PR's place in that stage's queue, and a rough ETA ("about 6 hours"). It
     says there is nothing for you to do.
   - **Waiting:** the author's move. It says what was asked and when, and when the
     PR closes as stale if that applies.
   - **Stalled:** your move, because the automation gave up or got stuck: a
     hand-back, a RED or YELLOW security review, a needs-human pick, a gated merge.
     It says what blocked it, who blocked it, and what was tried.
2. **The block has a sentence, a stage path and actions.**
   - One sentence leads.
   - Under it, a row of stages: threat scan → CI → code review → design review →
     security review → test run → ready to merge. Each is shown as done, current,
     failed, or not reached.
   - Then the actions that fit the state.
3. **Actions on a stalled PR:**
   - **🤖 Ask the agent why it's stalled.** This is the first action. It opens the
     agent sidebar on a new conversation, seeded with what the agent needs (below),
     and asks it to work out why the PR is stuck and recommend what to do.
   - **Re-attempt with my notes…** queues another attempt with the operator's
     guidance. For our fix PRs that's the follow-up send-back; for an outsider's PR
     it's an autofix request with guidance.
   - **Accept the blocker and continue**, where a blocker can be overridden (for
     example a reviewer's 4/5).
   - **Abandon:** close the PR.

   A moving PR may offer "run <stage> now", using the existing manual triggers
   (design review and security review jobs, queue for verification).
4. **Our own PRs never get author-style suggestions.** For an issue-fix PR, the
   status reads from its issue's follow-up record (`fix_followup`: its state, step,
   reason, `judged.by` and revisions). A hand-back is Stalled, owned by you. Nothing
   suggests commenting on it.
5. **A Status column in the PR Explorer, like the Issue Explorer's.** It shows the
   state badge and the same sentence. The current Status column (the automation
   reason) becomes this column, so there is one status per PR, not two.

## Seeding the agent

The seed is a plain-text first message, built by the backend, holding:
- the PR number, its title, and whether it's ours (with the issue it fixes) or an
  outsider's;
- the stage it stopped at, who stopped it, and their words;
- what was tried: the PR's recent ledger outcomes from the shared outcome log
  (`pipeline/outcomes.py`), newest first, capped to a few;
- the ask: why is it stuck, and what should happen (re-attempt with what notes,
  accept the blocker, or abandon)? Say which and why, and don't act.

The agent pane already opens with a question filled in (`useAgentPane().askAbout`, used
by the diff view's "Explain this code"). This needs a variant that starts a new
conversation, not one that adds to the current thread. The agent keeps its current
powers: it acts only through its confirm-first helpers.

## Non-goals

- No change to what any worker decides, retries or gives up on: the gates, #722's
  retry bounds, and the follow-up budgets stay as they are. The block reports;
  it doesn't steer.
- No new store table, section or registry key. Status is derived on read, like
  `automation.classify`.
- No new notifications.
- Closed PRs keep their current page.

## Anti-goals

- Two places deciding a PR's status. The sentence, the state and the owner come from
  one backend function, which the PR page, the Explorer column and Home all read.
  A second derivation in the frontend would fail the design.
- Offering "Ask the agent why it's stalled" or "Abandon" on a Ready or Needs you
  PR.
- An ETA presented as a promise. It's a rough estimate ("about"), and the block says
  "unknown" rather than invent one.
- A slower Explorer. The column must cost no more per row than today's
  `automation.classify`.

## Constraints

- Follow AGENTS.md: one owner per decision; store facts, not conclusions; type every
  signature; comments describe the present.
- Queue position must use the same pick order the workers use: `verify_worker`'s
  pools with `hunt_key` / `design_hunt_key`, and `fix_worker.hunt_candidates`.
  Don't build a second ordering.
- The ETA comes from measured lane throughput: completed runs per hour over a recent
  window, read from the PR runs ledger. It must be cheap on read. If it needs the
  ledger, compute it once per request for all PRs, not per row.
- Time the Explorer read and the PR page read against the configured store, before
  and after, and put the numbers in the PR description (architecture-review skill).

## Decided

- Five states: Moving, Waiting, Ready, Needs you, Stalled. The ETA is rough and
  qualified.
- "Ask the agent why it's stalled" is the first stalled action, starting a new seeded
  conversation.
- The PR Explorer gets the status as a column, replacing the reason-only Status
  column.
- Mockup: three sample cards (#17442 moving, #17534 stalled, an outsider's PR
  waiting), in `2026-10-28-pr-status-block-mockup.html` next to this file. Layout
  and tone are decided. The numbers and wording in it are samples.

## Builder's calls (settled in review, marked for Brandon to check)

- **Our PRs' status lives inside `classify`.** `automation.classify` reads the issue's
  follow-up for an issue-fix PR, so the PR page, the Explorer and Home agree. Home's
  counts move as a result (our hand-backs leave the author buckets). Finding the
  issue for a PR has to stay cheap per row: build a PR → issue map once from the
  in-memory issue snapshot (`issue_data`), not a store read per row.
- **Accept the blocker:**
  - On our PR, it marks the follow-up's hand-back accepted (the PR reads Ready for
    the maintainers) through the existing `fix_review` approve path.
  - On an outsider's PR, it's the existing human merge with its override. That path
    still runs `merge_eligibility`, the threat re-scan and the compile preflight.
  - No new stored override.
- **Abandon on our PR** closes the PR and ends the issue's fix attempt through
  `fix_review`, so the factory doesn't propose it again. On an outsider's PR it's
  the existing close.
- **Stage path:**
  - It gains "Analysis" first (summarized, clustered and analyzed) and "Conflicts"
    (merge conflicts or drift) before CI.
  - Security scans fold into "Code review" ("Reviews & scans").
  - For our PRs, the follow-up's blocker (CI, a reviewer, a maintainer) marks its
    stage failed.
- **"What was tried" reads both ledgers for one PR:** the PR ledger's outcomes and,
  for our PRs, the issue ledger's `issue-fix` / `issue-followup` outcomes for its
  issue. Both are in memory (`data.runs`, `issue_data.runs`). The page reads one PR,
  so this isn't per Explorer row. The ETA for a follow-up step comes from the
  issue-side throughput.

## Left to the builder

- **Mapping to states.** How `automation.classify`'s buckets map onto the five
  states:
  - auto → Moving;
  - author-owned → Waiting;
  - `merge-ready` → Ready;
  - the other act buckets (`approve-ci`, `rerun-ci`, `approve-parked`,
    `malicious`) → Needs you;
  - the handed buckets with you as owner → Stalled.

  Check every bucket.
- **Sentence home.** Whether the sentence extends `classify`'s `reason` or becomes a
  new field beside it.
- **Seed home.** Where the seed text is built, and its exact wording.
- **Stage path.** How it reads each stage's done/current/failed, reusing `pr_checks`.
- **Action endpoints.** Which existing endpoint each action calls.
- **Splitting the work.** Whether to land it as more than one PR, for example the
  backend status and Explorer column first, then the PR page block and the agent
  button.
