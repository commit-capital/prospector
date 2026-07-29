import { useEffect, useState } from "react";
import { api, type PRDetail, type ExecResult, type RunState } from "../api";
import { useExec } from "../ExecContext";
import { TagPicker } from "./TagPicker";
import { RunBadge } from "./RunBadge";
import { CommentEditor } from "./CommentEditor";
import { landed, ExecResultChip } from "./execResult";

const TONE_ICON = { green: "✅", yellow: "✋", red: "⛔", muted: "↪" } as const;

// Every action the operator can take on a PR, in one menu. Reopen is the
// always-visible ↩ button, not a row.
type Act = "MERGE" | "APPROVE" | "REQUEST_CHANGES" | "COMMENT"
  | "CLOSE" | "CLOSE_DUP" | "CLOSE_FIXED" | "CLOSE_STALE" | "CLOSE_OVERSIZED";

const OPTS: { v: Act; label: string }[] = [
  { v: "MERGE", label: "merge" },
  { v: "APPROVE", label: "approve" },
  { v: "REQUEST_CHANGES", label: "request changes" },
  { v: "COMMENT", label: "comment" },
  { v: "CLOSE", label: "triage close" },
  { v: "CLOSE_DUP", label: "close — dup of" },
  { v: "CLOSE_FIXED", label: "close — already-fixed" },
  { v: "CLOSE_STALE", label: "close — stale" },
  { v: "CLOSE_OVERSIZED", label: "close — too big (break up)" },
];

const isClose = (a: Act) => a.startsWith("CLOSE");
const isReviewEvent = (a: Act) => a === "APPROVE" || a === "REQUEST_CHANGES" || a === "COMMENT";
const reviewEvent = (a: Act) => a === "APPROVE" ? "approve" : a === "REQUEST_CHANGES" ? "request-changes" : "comment";

/** The agent's recommended disposition → the matching dropdown action, so the
 *  bar opens pre-selected on what the agent suggests. */
function suggestedAct(pr: PRDetail): Act {
  const a = pr.suggestion?.accept;
  if (!a) return "COMMENT";
  if (a.kind === "merge") return "MERGE";
  if (a.kind === "close") return (a.action as Act) || "CLOSE";
  // review
  return a.event === "approve" ? "APPROVE" : a.event === "request-changes" ? "REQUEST_CHANGES" : "COMMENT";
}

/** The canonical PR the agent identified for a close-dup, so the bar opens with
 *  the "#" already filled — and the comment preview cites it — instead of an
 *  empty box that previews the neutral "duplicate during triage" wording (#195). */
function suggestedCanonical(pr: PRDetail): string {
  const a = pr.suggestion?.accept;
  return a?.kind === "close" && a.canonical ? String(a.canonical) : "";
}

/** One action surface for the PR detail page: comment / approve / request-changes
 *  / merge / close, plus reopen. The agent's recommendation is folded in as the
 *  pre-selected action with its rationale; merge is gated by pr.merge_gate. */
export function PRActionBar({ pr, runState, onActed }:
  { pr: PRDetail; runState?: RunState; onActed?: () => void }) {
  const { botLogin, dryRun, canMergeUpstream, reportResult } = useExec();
  const [action, setAction] = useState<Act>(() => suggestedAct(pr));
  const [method, setMethod] = useState("squash");
  const [canonical, setCanonical] = useState(() => suggestedCanonical(pr));
  const [tags, setTags] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ExecResult | null>(null);
  // Comment the configured bot will post — pre-filled with the default for the action,
  // editable. null edit = use the default.
  const [defComment, setDefComment] = useState("");
  const [edited, setEdited] = useState<string | null>(null);
  const [openComment, setOpenComment] = useState(isReviewEvent(suggestedAct(pr)));

  const sug = pr.suggestion;
  const mergeGate = pr.merge_gate;
  const postsComment = action !== "MERGE";

  useEffect(() => { setResult(null); }, [dryRun]); // eslint-disable-line react-hooks/set-state-in-effect -- clear stale chip on mode flip
  useEffect(() => { setEdited(null); }, [action, canonical]); // eslint-disable-line react-hooks/set-state-in-effect -- edit was for the old wording
  useEffect(() => { setOpenComment(isReviewEvent(action)); }, [action]); // eslint-disable-line react-hooks/set-state-in-effect -- match the editor open-state to the action

  // Fetch the default comment for the chosen action. Closes get the executor's
  // default; request-changes gets the suggestion endpoint's rendered body (auto-
  // fills a leaked secret's `VAR in file`). For comment/approve we seed the
  // agent's suggested text when it matches the selected event, else blank — the
  // operator writes it. Merge posts nothing.
  useEffect(() => {
    let live = true;
    const seedFromSuggestion = sug && suggestedAct(pr) === action ? (sug.bot_comment ?? "") : "";
    let p: Promise<string>;
    if (action === "MERGE") p = Promise.resolve("");
    else if (action === "REQUEST_CHANGES") p = api.suggestForAction(pr.number, "request-changes").then((s) => s.bot_comment ?? "");
    // close-oversized renders through the suggestion endpoint so it can pull this
    // PR's cluster's merge PRs into the comment (#183).
    else if (action === "CLOSE_OVERSIZED") p = api.suggestForAction(pr.number, "close-oversized").then((s) => s.bot_comment ?? "");
    else if (isClose(action)) p = api.defaultComment(action, action === "CLOSE_DUP" && canonical ? Number(canonical) : undefined).then((d) => d.comment);
    else p = Promise.resolve(seedFromSuggestion); // comment / approve
    p.then((c) => { if (live) setDefComment(c || ""); }).catch(() => {});
    return () => { live = false; };
  }, [action, canonical, pr.number]); // eslint-disable-line react-hooks/exhaustive-deps

  const commentText = edited ?? defComment;
  const isEdited = edited != null && edited !== defComment;
  const needsCanon = action === "CLOSE_DUP" && !canonical;
  const needsBody = action === "REQUEST_CHANGES" && !commentText.trim();
  // An overridable gate — a YELLOW security verdict or an escalate verify
  // outcome — opens once the operator types a reason (🔒 box below); the backend
  // logs it to the right section (override_kind) before merging.
  const mergeOverride = action === "MERGE" && !mergeGate?.ok && !!mergeGate?.overridable && !!reason.trim();
  const overrideNoun = mergeGate?.override_kind === "verify"
    ? "verify-escalate override" : "security-YELLOW override";
  const mergeBlocked = action === "MERGE" && (!canMergeUpstream || !(mergeGate?.ok || mergeOverride));
  const alreadyDone = !dryRun && !!runState?.done;
  const blocked = needsCanon || needsBody || mergeBlocked;
  const toggle = (t: string) => setTags((s) => s.includes(t) ? s.filter((x) => x !== t) : [...s, t]);

  const fire = async () => {
    if (blocked) return;
    if (alreadyDone && !window.confirm(`#${pr.number} was already actioned at ${runState?.at}. Act again anyway?`)) return;
    let confirmMsg: string;
    if (action === "MERGE") confirmMsg = `⚠ PERMANENT: merge #${pr.number} upstream as ${botLogin}?${mergeOverride ? ` Your reason will be logged as the ${overrideNoun}.` : ""} This cannot be undone.`;
    else if (isClose(action)) confirmMsg = `Post this comment and close #${pr.number} as ${botLogin} (reopenable)?`;
    else confirmMsg = `Submit a "${reviewEvent(action)}" review on #${pr.number} as ${botLogin}?`;
    if (!dryRun && !window.confirm(confirmMsg)) return;

    setBusy(true);
    let r: ExecResult;
    if (action === "MERGE") {
      r = await api.mergePr(pr.number, dryRun, method, reason.trim() || undefined);
    } else if (isReviewEvent(action)) {
      r = await api.submitReview(pr.number, reviewEvent(action), commentText, dryRun, reason || undefined, tags);
    } else {
      r = await api.executePr(pr.number, {
        pr: pr.number, action, canonical: canonical ? Number(canonical) : undefined,
        tags, reason: reason || undefined, comment: commentText || undefined,
      }, dryRun);
    }
    setResult(r);
    setBusy(false);
    reportResult(r);
    if (landed(r)) onActed?.();
  };

  const reopen = async () => {
    if (!dryRun && !window.confirm(`Reopen #${pr.number}, remove the bot's closing comment, and withdraw any change request?`)) return;
    setBusy(true);
    const r = await api.reopenPr(pr.number, dryRun);
    setResult(r);
    setBusy(false);
    reportResult(r);
    if (landed(r)) onActed?.();
  };

  // stable-width fire label (spinner shows busy, so width never jumps)
  const fireLabel = (() => {
    if (action === "MERGE") return !(mergeGate?.ok || mergeOverride) ? "⛔ Merge blocked" : !canMergeUpstream ? "⛔ can't merge" : dryRun ? "Merge (dry)" : "● Merge";
    if (action === "APPROVE") return dryRun ? "Approve (dry)" : "● Approve";
    if (action === "REQUEST_CHANGES") return dryRun ? "Request changes (dry)" : "✎ Request changes";
    if (action === "COMMENT") return dryRun ? "Comment (dry)" : "● Comment";
    return alreadyDone ? "↻ re-close" : dryRun ? "Close (dry)" : "● Close";
  })();
  const fireTitle = mergeBlocked
    ? (!canMergeUpstream ? `No ${botLogin} token on this machine — merges run as ${botLogin} (dry-run only).` : mergeGate?.reason || "merge gate")
    : needsCanon ? "enter the canonical PR #" : needsBody ? "a request-changes review needs a comment"
    : alreadyDone ? `already actioned at ${runState?.at} — click to act again` : "";

  return (
    <div className={`suggestion tone-${sug?.tone ?? "muted"}`}>
      {/* agent recommendation, folded in (no separate banner) */}
      {sug && (
        <div className="sug-row">
          <div className="sug-main">
            <span className="sug-label">
              {TONE_ICON[sug.tone]} Agent suggests: <b>{sug.label}</b>
              {sug.accept && (sug.reversible
                ? <span className="rev-badge rev-ok" title="Reversible — closes can be reopened, comments deleted.">reversible</span>
                : <span className="rev-badge rev-perm" title="PERMANENT — a merge cannot be undone. Review carefully.">⚠ permanent</span>)}
              {runState && <> <RunBadge rs={runState} compact /></>}
            </span>
            <span className="sug-why">{sug.rationale}</span>
          </div>
        </div>
      )}
      {sug?.needs_verify && (
        <div className="sug-verify" title="The AI's evidence here is thin — check it before firing.">⚠ Needs your check: {sug.needs_verify}</div>
      )}

      {/* the one action row */}
      <div className="pr-actions-wrap stacked">
        <div className="pr-actions">
          <select value={action} onChange={(e) => setAction(e.target.value as Act)} disabled={busy}>
            {OPTS.map((o) => <option key={o.v} value={o.v}>{o.label}</option>)}
          </select>
          {action === "MERGE" && (
            <select value={method} onChange={(e) => setMethod(e.target.value)} disabled={busy} title="Merge method">
              <option value="squash">squash</option>
              <option value="merge">merge</option>
              <option value="rebase">rebase</option>
            </select>
          )}
          {action === "CLOSE_DUP" && (
            <input className="canon" placeholder="#" value={canonical} onChange={(e) => setCanonical(e.target.value.replace(/\D/g, ""))} disabled={busy} />
          )}
          <button className={`btn-primary sm btn-stable ${busy ? "is-busy" : ""} ${!dryRun ? "btn-live" : ""}`}
            onClick={fire} disabled={busy || blocked} title={fireTitle}>
            <span className="btn-stable-label">{fireLabel}</span>
            {busy && <span className="spinner btn-stable-spinner" aria-hidden="true" />}
          </button>
          <button className="btn-secondary sm" onClick={reopen} disabled={busy} title="Undo: reopen + remove bot comment + withdraw any change request">↩ Reopen</button>
          {runState && !result && <RunBadge rs={runState} compact />}
          {result && <ExecResultChip result={result} />}
        </div>

        {action === "MERGE" && !mergeGate?.ok && mergeGate?.reason && (
          <div className="sug-verify" title="gates.merge_eligibility — this PR hasn't passed every check we ran on it.">
            {mergeOverride
              ? <>⚠ {mergeGate.override_kind === "verify" ? "verify escalated" : "security YELLOW"} — merging logs your reason as the override</>
              : <>⛔ Merge blocked — {mergeGate.reason}{mergeGate.overridable ? " — type an override reason below (🔒 reason + note) to merge anyway" : ""}</>}
          </div>
        )}
        {action === "MERGE" && mergeGate?.ok && (
          <div className="sug-comment muted-note">↳ merges via {method} — no comment is posted.</div>
        )}

        {/* comment editor — every action that posts a comment */}
        {postsComment && (
          <CommentEditor botLogin={botLogin} value={commentText} isEdited={isEdited}
            open={openComment} onToggle={() => setOpenComment((o) => !o)}
            onChange={setEdited} onReset={() => setEdited(null)}
            placeholder={action === "REQUEST_CHANGES" ? "what needs to change (required)" : "comment posted to the PR (optional)"} />
        )}

        {/* private reason tags + note — training data, never posted */}
        <div className="pr-tags">
          <span className="muted small">{action === "MERGE" && mergeGate?.overridable && !mergeGate?.ok
            ? `🔒 override reason (private — logged to the store as the ${overrideNoun}, never posted):`
            : "🔒 reason + note (private, optional — training data, not posted):"}</span>
          <TagPicker selected={tags} onToggle={toggle} />
          <textarea className="modal-text" rows={2} placeholder="optional note: e.g. 'merging despite no security review — Easy-Lane, trusted author, cold path'"
            value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>
      </div>
    </div>
  );
}
