import { useState } from "react";

import type { FixAction, FixRequest, FixRunner } from "../api";
import { useExec } from "../ExecContext";
import { localDateTime, timeAgo } from "../timeAgo";

/** What the composer starts with. The operator edits the goal; the bar beneath
 *  it is not theirs to delete, so it lives in the agent's prompt and is shown
 *  here only so they know what the change is being held to. */
const DEFAULT_GOAL = "Fix the outstanding review feedback on this PR.";
const PINNED_BAR = "Always applied: succeed only if the change is safe and "
                 + "well-constructed, with no risk of introducing a bug or "
                 + "destabilizing the system. Give up rather than guess.";

/** Why an action cannot be queued right now, or null when it can be.
 *  Queueing is gated on a worker being reachable, NOT on this browser's backend
 *  holding the push key — the queue is shared so an operator clicks from a
 *  laptop and the machine with the key does the work. Eligibility itself is the
 *  backend's call (gates.fix_eligibility); it reports its reason on the click
 *  rather than being second-guessed here. */
function unavailable(runner: FixRunner | null, req: FixRequest | null,
                     resolved: boolean): string | null {
  if (resolved) return "This PR is already resolved.";
  if (runner != null && !runner.can_queue) {
    return "No autofix worker has been seen against this store, so a queued action "
         + "would never run. Start a backend with TRIAGE_FIX_WORKER=1 on the machine "
         + "holding the machine user's push key.";
  }
  const status = req?.status;
  if (status && IN_FLIGHT.includes(status)) {
    return `PR already has a ${status} ${req?.action} request.`;
  }
  return null;
}

const IN_FLIGHT = ["queued", "running", "awaiting-review", "approved", "pushing"];

const LABEL: Record<FixAction, string> = {
  update: "↻ Re-test against current main",
  rebase: "⟳ Resolve merge conflicts",
  fix: "🔧 Author a fix",
  describe: "📝 Rewrite the description",
};

const HELP: Record<FixAction, string> = {
  update: "Adds a merge commit bringing current main into this PR, which makes CI "
        + "and the review provider answer \"does this still work against main as it "
        + "is today?\" — their last answer is only as current as the last push. "
        + "Does not unblock a merge: GitHub merges cleanly with or without this. "
        + "The contributor's commits are untouched, and a conflicting main stops "
        + "before anything is pushed. Moving the head does re-stale this PR's "
        + "stored facts, so it needs a re-ingest afterwards.",
  rebase: "Replays this PR's commits onto current main to clear a conflict, then "
        + "force-pushes behind a lease pinned to the author's exact head. The "
        + "contributor stays the author of every commit, but the commits get new "
        + "SHAs — which collapses inline review comments anchored to the old ones, "
        + "and means anyone with the branch checked out needs a hard reset. It "
        + "parks for your review before any of that happens.",
  fix: "Has an agent write a change against this PR and park the diff for your "
     + "review — nothing is pushed until you approve it. You say what to fix; a "
     + "second agent that did not write it has to fail to find a reason to "
     + "reject it, and the change has to compile, before it reaches your queue.",
  describe: "Has an agent rewrite the PR description to follow the repository's "
          + "pull request template, from the diff and the author's own text (kept "
          + "verbatim). It parks for your review; approving posts it as the bot and "
          + "asks the reviewer to look again. For PRs whose only review finding is "
          + "the description itself.",
};

/** The queue/run state strip: where an in-flight, parked, or failed autofix
 *  request stands. */
function RequestStrip({ req, runner }: { req: FixRequest; runner: FixRunner | null }) {
  const offline = runner != null && !runner.online;
  const offlineChip = offline && (
    <span className="chip chip-yellow sm" style={{ marginLeft: 8 }}
      title="No autofix worker has beat recently, so the queue is not draining. Start the app backend with TRIAGE_FIX_WORKER=1 on the machine holding the push key.">
      ⚠ runner offline
    </span>
  );
  const when = req.queued_at && (
    <span title={localDateTime(req.queued_at)}> · queued {timeAgo(req.queued_at)}</span>
  );

  if (req.status === "queued") {
    return (
      <div className="verdict-banner v-unknown">
        <span className="vb-icon">⏳</span>
        <div>
          <div className="vb-headline">Queued to {req.action}{offlineChip}</div>
          <div className="vb-detail">
            Waiting for the runner{req.source === "auto" && " · auto-picked"}{when}
          </div>
        </div>
      </div>
    );
  }
  if (req.status === "running" || req.status === "pushing") {
    return (
      <div className="verdict-banner v-unknown">
        <span className="vb-icon">🔄</span>
        <div>
          <div className="vb-headline">
            {req.status === "pushing" ? "Pushing" : "Running"} {req.action}
            {req.step ? ` — ${req.step}` : ""}
          </div>
          <div className="vb-detail">
            {req.started_at && <span title={localDateTime(req.started_at)}>started {timeAgo(req.started_at)}</span>}
            {req.host && ` · on ${req.host}`}
          </div>
        </div>
      </div>
    );
  }
  if (req.status === "awaiting-review" || req.status === "approved") {
    const pf = req.result?.compile_preflight;
    const proven = pf == null || pf.exit === 0;
    const mechanical = req.action === "update" || req.action === "rebase";
    return (
      <div className={proven ? "verdict-banner v-green" : "verdict-banner v-caution"}>
        <span className="vb-icon">{proven ? "✅" : "👀"}</span>
        <div>
          <div className="vb-headline">
            {req.status === "approved"
              ? "Approved — waiting for the runner to push"
              : req.action === "resolve"
                ? "An agent resolved the merge conflicts — review & approve"
                : req.action === "describe"
                  ? "A new PR description is ready — review & approve"
                : proven
                  ? `Conflicts resolvable — this ${req.action} applies cleanly`
                  : `A ${req.action} is ready for your review`}
            {req.status === "approved" && offlineChip}
          </div>
          <div className="vb-detail">
            {req.action === "describe"
              ? "Nothing has been posted yet; approving edits the description as the bot."
              : "Nothing has been pushed to the contributor's branch yet."}
            {pf && pf.exit === 0 && " Compile preflight passed."}
            {req.result?.message && ` Commit message: “${req.result.message}”.`}
            {req.action === "resolve" &&
              " The conflicted hunks are in the Diff panel below — switch it to “Merge diff”."}
            {mechanical && req.base_sha &&
              ` Proven against base ${req.base_sha.slice(0, 8)}; pushing re-runs it against
                current base first.`}
          </div>
          {req.result?.auto_review?.bar && (
            <div className="vb-detail" style={{ marginTop: 4 }}>
              {req.result.auto_review.bar.ok
                ? "🤖 Auto-review cleared it: two agent reviewers found nothing wrong"
                  + (req.result.auto_review.tests
                    ? " and the related tests passed."
                    : "; no related tests were found to run.")
                : `🤖 Auto-review left it for you: ${req.result.auto_review.bar.reason}`}
            </div>
          )}
        </div>
      </div>
    );
  }
  if (req.status === "refused") {
    return (
      <div className="verdict-banner v-caution">
        <span className="vb-icon">⛔</span>
        <div>
          <div className="vb-headline">Nothing was pushed</div>
          <div className="vb-detail">
            {req.refused_reason ?? "No reason was recorded."}
            {req.result?.merge_diff && (
              <> The conflicted hunks are in the Diff panel below — switch it to “Merge diff”.</>
            )}
          </div>
        </div>
      </div>
    );
  }
  if (req.status === "failed") {
    return (
      <div className="verdict-banner v-red">
        <span className="vb-icon">✗</span>
        <div>
          <div className="vb-headline">This didn't finish</div>
          <div className="vb-detail">{req.error ?? "No error was recorded."}</div>
        </div>
      </div>
    );
  }
  if (req.status === "pushed") {
    return (
      <div className="verdict-banner v-green">
        <span className="vb-icon">✓</span>
        <div>
          <div className="vb-headline">
            Pushed {req.action} as {runner?.push_login ?? "the contributor-push user"}
          </div>
          <div className="vb-detail">
            The review provider and CI re-run on the push.
            {req.finished_at && <span title={localDateTime(req.finished_at)}> · {timeAgo(req.finished_at)}</span>}
          </div>
        </div>
      </div>
    );
  }
  return null;
}

/** The action bar's autofix controls: queue an action, cancel a queued one, or
 *  approve/discard one parked for review. */
export function FixAction({ req, runner, busy, resolved, onQueue, onDequeue, onApprove }: {
  req: FixRequest | null;
  runner: FixRunner | null;
  busy: FixAction | "dequeue" | "approve" | null;
  resolved: boolean;
  onQueue: (action: FixAction, guidance?: string) => void;
  onDequeue: () => void;
  onApprove: () => void;
}) {
  const { dryRun } = useExec();
  const [goal, setGoal] = useState<string | null>(null);
  const status = req?.status;
  if (status === "running" || status === "pushing") {
    return <span className="chip chip-blue sm" title={`An autofix ${req?.action} is in flight for this PR.`}>
      {req?.action}…
    </span>;
  }
  if (status === "awaiting-review") {
    return (
      <>
        <button className="btn-primary sm" disabled={busy != null} onClick={onApprove}
          title={dryRun
            ? "Dry run: preview what approving would push — nothing reaches GitHub until you switch to Live."
            : "Push the authored change to the contributor's branch. This is the first moment anything reaches GitHub."}>
          {busy === "approve" ? "Approving…"
            : dryRun ? "✓ Approve & push (dry run)" : "✓ Approve & push"}
        </button>
        <button className="btn-secondary sm" disabled={busy != null} onClick={onDequeue}
          title="Discard the authored change without pushing it.">
          {busy === "dequeue" ? "Discarding…" : "✕ Discard"}
        </button>
      </>
    );
  }
  if (status === "queued") {
    return (
      <button className="btn-secondary sm" disabled={busy != null} onClick={onDequeue}
        title="Remove this PR from the autofix queue before the runner picks it up.">
        {busy === "dequeue" ? "Cancelling…" : "✕ Cancel queued autofix"}
      </button>
    );
  }
  const why = unavailable(runner, req, resolved);
  if (goal != null) {
    return (
      <div className="fix-composer">
        <textarea className="fix-goal" rows={3} value={goal} autoFocus
          placeholder={DEFAULT_GOAL} onChange={(e) => setGoal(e.target.value)}
          aria-label="What the agent should fix" />
        <div className="muted small">{PINNED_BAR}</div>
        <div className="row-actions">
          <button className="btn-primary sm" disabled={busy != null || !goal.trim()}
            onClick={() => onQueue("fix", goal.trim())}
            title="Queue this for the autofix worker. The agent writes the change, a
                   second agent reviews it, and it parks here for your approval.">
            {busy === "fix" ? "Queuing…" : "🔧 Author this fix"}
          </button>
          <button className="btn-secondary sm" disabled={busy != null}
            onClick={() => setGoal(null)}>Cancel</button>
        </div>
      </div>
    );
  }
  return (
    <>
      {(["update", "rebase", "fix", "describe"] as FixAction[]).map((action) => (
        <button key={action} className="btn-secondary sm"
          disabled={busy != null || why != null}
          onClick={() => (action === "fix" ? setGoal(DEFAULT_GOAL) : onQueue(action))}
          title={why ?? HELP[action]}>
          {busy === action ? "Queuing…" : LABEL[action]}
        </button>
      ))}
    </>
  );
}

/** The expanded autofix detail: the request's live state plus, once one is
 *  parked for review, the exact diff the runner authored. The diff is shown
 *  before anything is pushed — approving is what sends it. */
export function FixBody({ req, runner }: { req: FixRequest | null; runner: FixRunner | null }) {
  if (!req) {
    return (
      <div className="muted small">
        No autofix has been queued for this PR. Queueing one has the
        {runner?.push_login ? ` ${runner.push_login}` : " contributor-push"} account act on
        the contributor's branch.
        {runner != null && !runner.can_queue
          && " No worker has been seen against this store, so nothing would run yet."}
      </div>
    );
  }
  const pf = req.result?.compile_preflight;
  const review = req.result?.review_verdict;
  const showPatch = req.result?.patch
    && (req.status === "awaiting-review" || req.status === "approved");
  // Raw command output, build errors and stack traces are for whoever is
  // debugging the worker. They go behind a disclosure so the plain explanation
  // above stays the thing an operator reads.
  const raw = [pf?.error_excerpt, pf?.error, req.result?.detail, req.result?.output]
    .filter(Boolean).join("\n\n");
  return (
    <>
      <RequestStrip req={req} runner={runner} />
      {req.guidance && (
        <div className="small" style={{ marginTop: 8 }}>
          <span className="muted">You asked for: </span>{req.guidance}
        </div>
      )}
      {req.result?.resolutions && req.result.resolutions.length > 0 && (
        <div className="small" style={{ marginTop: 8 }}>
          <div className="muted">How each conflict was resolved:</div>
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {req.result.resolutions.map((r) => (
              <li key={r.path}><code>{r.path}</code> — {r.rationale}</li>
            ))}
          </ul>
        </div>
      )}
      {req.result?.changes && req.result.changes.length > 0 && (
        <div className="small" style={{ marginTop: 8 }}>
          <div className="muted">What the agent changed:</div>
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {req.result.changes.map((c) => (
              <li key={c.path}><code>{c.path}</code> — {c.rationale}</li>
            ))}
          </ul>
        </div>
      )}
      {review && (
        <div className="small" style={{ marginTop: 8 }}>
          <span className="muted">
            {review.verdict === "safe"
              ? "A reviewing agent looked for a reason to reject this and found none: "
              : "A reviewing agent rejected this: "}
          </span>
          {review.reason}
          {review.concerns.length > 0 && (
            <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              {review.concerns.map((c) => <li key={c}>{c}</li>)}
            </ul>
          )}
        </div>
      )}
      {showPatch && (
        <>
          <div className="small muted" style={{ marginTop: 8 }}>
            The change, exactly as it would be pushed:
          </div>
          <pre className="log-tail">{req.result?.patch}</pre>
        </>
      )}
      {req.result?.body && (req.status === "awaiting-review" || req.status === "approved") && (
        <>
          <div className="small muted" style={{ marginTop: 8 }}>
            The description, exactly as it would be posted:
          </div>
          <pre className="log-tail" style={{ whiteSpace: "pre-wrap" }}>{req.result.body}</pre>
        </>
      )}
      {raw && (
        <details className="small" style={{ marginTop: 8 }}>
          <summary className="muted">Technical detail</summary>
          <pre className="log-tail">{raw}</pre>
        </details>
      )}
    </>
  );
}
