import type { FixAction, FixRequest, FixRunner } from "../api";
import { localDateTime, timeAgo } from "../timeAgo";

/** Why a given action is unavailable right now, or null when it may be queued.
 *  A machine with no push identity can never push, so every action is withheld
 *  with that reason rather than failing at the runner. */
function unavailable(action: FixAction, runner: FixRunner | null,
                     req: FixRequest | null, resolved: boolean): string | null {
  if (resolved) return "This PR is already resolved.";
  if (runner != null && !runner.push_identity) {
    return "No machine user is configured on this deployment, so nothing can push to "
         + "contributor branches. Set TRIAGE_PUSH_LOGIN, TRIAGE_PUSH_EMAIL and "
         + "TRIAGE_PUSH_SSH_KEY_FILE in .env.";
  }
  const status = req?.status;
  if (status && IN_FLIGHT.includes(status)) {
    return `PR already has a ${status} ${req?.action} request.`;
  }
  if (action === "fix") {
    return null; // the backend's profile opt-in is the authority; it reports why
  }
  return null;
}

const IN_FLIGHT = ["queued", "running", "awaiting-review", "approved", "pushing"];

const LABEL: Record<FixAction, string> = {
  update: "⤵ Merge base in",
  rebase: "⟳ Rebase onto base",
  fix: "🔧 Author a fix",
};

const HELP: Record<FixAction, string> = {
  update: "Merge the base branch into this PR's head so CI and the review provider "
        + "re-run against current base code. Authors no content of its own, and a "
        + "conflicting base stops before anything is pushed.",
  rebase: "Replay this PR's commits onto current base to clear a conflict, then "
        + "force-push behind a lease pinned to the author's exact head. Rewrites "
        + "the contributor's branch history, so it parks for your review first.",
  fix: "Have an agent author a change against this PR's failing gates, then park "
     + "the diff for your review. Only the gates the repository profile names as "
     + "fixable are attempted.",
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
    return (
      <div className="verdict-banner v-caution">
        <span className="vb-icon">👀</span>
        <div>
          <div className="vb-headline">
            {req.status === "approved"
              ? `Approved — waiting for the runner to push${runner?.online ? "" : ""}`
              : `A ${req.action} is ready for your review`}
            {req.status === "approved" && offlineChip}
          </div>
          <div className="vb-detail">
            Nothing has been pushed to the contributor's branch yet.
            {pf && pf.exit === 0 && " Compile preflight passed."}
            {req.result?.message && ` Commit message: “${req.result.message}”.`}
          </div>
        </div>
      </div>
    );
  }
  if (req.status === "refused") {
    return (
      <div className="verdict-banner v-caution">
        <span className="vb-icon">⛔</span>
        <div>
          <div className="vb-headline">The {req.action} was refused — nothing was pushed</div>
          <div className="vb-detail">{req.refused_reason ?? "no reason recorded"}</div>
        </div>
      </div>
    );
  }
  if (req.status === "failed") {
    return (
      <div className="verdict-banner v-red">
        <span className="vb-icon">✗</span>
        <div>
          <div className="vb-headline">The {req.action} failed</div>
          <div className="vb-detail">{req.error ?? "no error recorded"}</div>
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
            Pushed {req.action} as {runner?.push_login ?? "the machine user"}
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
  onQueue: (action: FixAction) => void;
  onDequeue: () => void;
  onApprove: () => void;
}) {
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
          title="Push the authored change to the contributor's branch. This is the first moment anything reaches GitHub.">
          {busy === "approve" ? "Approving…" : "✓ Approve & push"}
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
  return (
    <>
      {(["update", "rebase", "fix"] as FixAction[]).map((action) => {
        const why = unavailable(action, runner, req, resolved);
        return (
          <button key={action} className="btn-secondary sm" disabled={busy != null || why != null}
            onClick={() => onQueue(action)} title={why ?? HELP[action]}>
            {busy === action ? "Queuing…" : LABEL[action]}
          </button>
        );
      })}
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
        {runner?.push_login ? ` ${runner.push_login}` : " machine user"} account act on the
        contributor's branch — never your own account.
      </div>
    );
  }
  const pf = req.result?.compile_preflight;
  return (
    <>
      <RequestStrip req={req} runner={runner} />
      {pf && (pf.refused || pf.error || (pf.exit != null && pf.exit !== 0)) && (
        <div className="small">
          <strong>Compile preflight:</strong>{" "}
          {pf.refused ?? pf.error ?? `exited ${pf.exit}`}
          {pf.error_excerpt && <pre className="log-tail">{pf.error_excerpt}</pre>}
        </div>
      )}
      {req.result?.patch && (
        <>
          <div className="small muted" style={{ marginTop: 8 }}>
            The authored change, exactly as it would be pushed:
          </div>
          <pre className="log-tail">{req.result.patch}</pre>
        </>
      )}
      {req.result?.output && <pre className="log-tail">{req.result.output}</pre>}
    </>
  );
}
