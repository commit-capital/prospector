import { useEffect, useRef, useState } from "react";
import { api, type Suggestion, type ExecResult, type HumanMerge, type RunState } from "../api";
import { useExec } from "../ExecContext";
import { RunBadge } from "./RunBadge";

const TONE_ICON = { green: "✅", yellow: "✋", red: "⛔", muted: "↪" } as const;

/** Shows the agent's recommended disposition for a PR + a one-click Accept that
 *  DOES the action (close / comment / request-changes / merge). Surfaces the
 *  EXACT comment that will be posted, whether it's reversible, and any reason
 *  it warrants a human look before firing. */
export function AgentSuggestion({ pr, suggestion, compact = false, commentSameAs, humanMerge, runState, onActed,
  editedComment, onEdit, onApplyToAll, applyToAllCount = 0, actionChanged = false }:
  { pr: number; suggestion?: Suggestion; compact?: boolean; commentSameAs?: number; humanMerge?: HumanMerge | null;
    runState?: RunState; onActed?: () => void;
    // controlled comment-editing: the cluster view lifts the edit up so it
    // reaches the bulk executor and can be applied to duplicate PRs (#64/#65).
    // Omit these and the component manages the edit locally (PR-detail view).
    editedComment?: string | null; onEdit?: (text: string | null) => void;
    onApplyToAll?: (text: string) => void; applyToAllCount?: number;
    // true once the operator overrides the disposition, so the suggested wording
    // was regenerated — used to warn a pre-existing edit may be stale (#13).
    actionChanged?: boolean }) {
  const { botLogin, dryRun, canMergeUpstream, reportResult } = useExec();
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [result, setResult] = useState<ExecResult | null>(null);
  // Operator-edited override of the comment the configured bot will post (#16/#23);
  // null = use the agent's suggested text verbatim. Controlled by the parent
  // when onEdit is supplied (cluster view), else managed locally.
  const [localEdited, setLocalEdited] = useState<string | null>(null);
  const controlled = !!onEdit;
  const edited = controlled ? (editedComment ?? null) : localEdited;
  const setEdited = (v: string | null) => { if (controlled) onEdit!(v); else setLocalEdited(v); };
  const sbc = suggestion?.bot_comment;
  const editedFromSuggestion = edited != null && edited !== (sbc ?? "");
  // The edit predates a regenerated suggestion only when the operator actually
  // overrode the disposition; a comment that arrives already-edited (pre-seeded
  // from a review link, restored, etc.) with no override is not stale.
  const staleEdit = actionChanged;
  // an edited comment reveals its box by default so the changed text is visible
  // without a click — except a duplicate that collapses to "same as #X"
  useEffect(() => { if (editedFromSuggestion && !commentSameAs) setOpen(true); }, [editedFromSuggestion, commentSameAs]); // eslint-disable-line react-hooks/set-state-in-effect -- reveal edited text
  // grow the textarea to fit its whole content, so a long comment shows in full
  const taRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${ta.scrollHeight}px`;
  }, [edited, sbc, open]);
  // toggling dry-run clears the leftover result chip so LIVE mode never shows a
  // stale dry-run outcome (#67)
  useEffect(() => { setResult(null); }, [dryRun]); // eslint-disable-line react-hooks/set-state-in-effect -- clear the stale result chip on dry-run/live flip
  if (!suggestion) return null;
  const { label, tone, rationale, accept, bot_comment, reversible, needs_verify } = suggestion;
  const isMerge = accept?.kind === "merge";
  const commentText = edited ?? bot_comment ?? "";
  const isEdited = editedFromSuggestion;
  // CODEOWNERS-gated PRs must be merged by a human owner — never the bot (#15/#26)
  const manualMerge = isMerge && !!humanMerge?.required;
  const mergeBlocked = isMerge && (!canMergeUpstream || manualMerge);

  // already-landed guard (#10): if a live action already ran on this PR, make
  // re-firing a deliberate choice — the operator who's editing everything as
  // they go shouldn't double-act by reflex.
  const alreadyDone = !dryRun && !!runState?.done;

  const doAccept = async () => {
    if (!accept || manualMerge) return;
    if (alreadyDone && !window.confirm(
      `#${pr} was already ${runState?.kind === "merge" ? "merged" : "closed"} at ${runState?.at}. Act again anyway?`)) return;
    setBusy(true);
    let res: ExecResult;
    if (accept.kind === "merge") {
      if (!dryRun && !window.confirm(`⚠ PERMANENT: merge #${pr} upstream as ${botLogin}? This cannot be undone.`)) { setBusy(false); return; }
      res = await api.mergePr(pr, dryRun, accept.method || "squash");
    } else if (accept.kind === "close") {
      if (!dryRun && !window.confirm(`Close #${pr} as ${botLogin} (reopenable)?`)) { setBusy(false); return; }
      res = await api.executePr(pr, {
        pr, action: accept.action!, canonical: accept.canonical,
        upstream_pr: accept.upstream_pr, upstream_commit: accept.upstream_commit,
        upstream_date: accept.upstream_date, tags: accept.tags,
        comment: commentText || undefined,   // edited text overrides the default
      }, dryRun);
    } else {
      if (!dryRun && !window.confirm(`${accept.event} #${pr} as ${botLogin} (deletable)?`)) { setBusy(false); return; }
      res = await api.submitReview(pr, accept.event || "comment", commentText || accept.body || "", dryRun, undefined, accept.tags);
    }
    setResult(res);
    setBusy(false);
    reportResult(res);
    if (res?.status === "executed" || res?.status === "merged" || res?.status === "reopened") onActed?.();
  };

  // inline undo right where you closed it (#69) — reopen + remove the bot comment
  const undo = async () => {
    // a live undo posts upstream (reopen + delete comment + dismiss review), so
    // it gets the same confirmation as every other live action here
    if (!dryRun && !window.confirm(
      `Reopen #${pr}, remove the bot's closing comment, and withdraw any change request?`)) return;
    setBusy(true);
    const r = await api.reopenPr(pr, dryRun);
    setBusy(false);
    setResult(r);
    reportResult(r);
    if (r?.status === "reopened") onActed?.();
  };

  return (
    <div className={`suggestion tone-${tone} ${compact ? "compact" : ""}`}>
      <div className="sug-row">
        <div className="sug-main">
          <span className="sug-label">
            {TONE_ICON[tone]} Agent suggests: <b>{label}</b>
            {accept && (reversible
              ? <span className="rev-badge rev-ok" title="Reversible — closes can be reopened, comments deleted.">reversible</span>
              : <span className="rev-badge rev-perm" title="PERMANENT — a merge cannot be undone. Review carefully.">⚠ permanent</span>)}
            {runState && <> <RunBadge rs={runState} compact /></>}
          </span>
          {!compact && <span className="sug-why">{rationale}{mergeBlocked && (manualMerge ? " · (CODEOWNERS — needs a human owner)" : ` · (no ${botLogin} token on this machine — dry-run only)`)}</span>}
        </div>
        {accept && (
          <button className={`btn-primary sm btn-stable ${busy ? "is-busy" : ""} ${!dryRun ? "btn-live" : ""}`} onClick={doAccept} disabled={busy || manualMerge}
            title={manualMerge ? "Requires a human code-owner merge — CODEOWNERS path."
              : mergeBlocked ? `No ${botLogin} token on this machine — merges run as ${botLogin} and need the bot key (dry-run only).` : ""}>
            <span className="btn-stable-label">{result ? "✓ done" : manualMerge ? "⛔ manual merge" : alreadyDone ? "↻ act again" : dryRun ? "Accept (dry)" : "● Accept"}</span>
            {busy && <span className="spinner btn-stable-spinner" aria-hidden="true" />}
          </button>
        )}
        {result && <span className={`chip chip-${result.status === "executed" || result.status === "merged" ? "green" : result.status === "error" || result.status === "blocked" ? "red" : "muted"}`} title={result.detail}>{result.status}</span>}
        {result?.status === "executed" && accept?.kind === "close" && (
          <button className="btn-secondary sm" disabled={busy} onClick={undo} title="Reopen this PR, remove the bot's closing comment, and withdraw any change request">↩ undo</button>
        )}
      </div>

      {manualMerge && humanMerge && (
        <div className="sug-manual" title="The upstream repo's branch ruleset requires a code owner to approve/merge these paths.">
          ⛔ <b>Requires human merge — touches CODEOWNERS-gated code.</b>{" "}
          Ping {humanMerge.owners.join(" + ")} to merge.
          <div className="sug-manual-paths">{humanMerge.paths.slice(0, 6).join(" · ")}{humanMerge.paths.length > 6 ? " …" : ""}</div>
        </div>
      )}

      {needs_verify && (
        <div className="sug-verify" title="The AI's evidence here is thin — check it before firing.">⚠ Needs your check: {needs_verify}</div>
      )}

      {bot_comment ? (
        commentSameAs && !open ? (
          <div className="sug-comment muted-note">
            📮 same comment as #{commentSameAs}
            <button className="sug-comment-edit" onClick={() => setOpen(true)}>edit</button>
          </div>
        ) : (
          <div className="sug-comment">
            <button className="sug-comment-toggle" onClick={() => setOpen((o) => !o)}>
              📮 {botLogin} will post {isEdited && <span className="sug-edited" title="You edited this from the suggested wording">✎ edited</span>} {open ? "▾" : "▸"}
              {!open && <span className="sug-comment-peek"> {commentText.replace(/\s+/g, " ").slice(0, 64)}…</span>}
            </button>
            {open && (
              <div className="sug-comment-edit-box">
                {staleEdit && isEdited && (
                  <div className="sug-stale-edit" title="The action changed, so the suggested wording was regenerated.">
                    ⚠ The action changed — the suggestion was updated, but you're still posting your earlier edit.
                    Reset to use the new suggested wording.
                  </div>
                )}
                <textarea ref={taRef} className="sug-comment-textarea" value={commentText} rows={3}
                  onChange={(e) => setEdited(e.target.value)}
                  aria-label={`Comment ${botLogin} will post`} />
                <div className="sug-comment-actions">
                  <button className="btn-secondary sm" disabled={!isEdited} onClick={() => setEdited(null)}
                    title="Restore the agent-suggested wording">↺ Reset to suggested</button>
                  {onApplyToAll && applyToAllCount > 0 && (
                    <button className="btn-secondary sm" disabled={!commentText.trim()}
                      onClick={() => onApplyToAll(commentText)}
                      title="Use this exact comment for the other PRs that currently share it">
                      📋 apply to {applyToAllCount} other{applyToAllCount === 1 ? "" : "s"}
                    </button>
                  )}
                  {isEdited && <span className="muted small">posting your edited text, not the suggestion</span>}
                </div>
              </div>
            )}
          </div>
        )
      ) : isMerge ? (
        <div className="sug-comment muted-note">↳ merges via squash — no comment is posted.</div>
      ) : null}
    </div>
  );
}
