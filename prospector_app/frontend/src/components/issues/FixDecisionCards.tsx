import { useState } from "react";
import { api, type IssueDetail } from "../../api";
import { useExec } from "../../ExecContext";
import { type FixDecision, sendBackDraft } from "./fixDecision";

/** The Summary's two answers to a fix PR waiting on the operator, side by side:
 *  approve it as it stands and leave it to the maintainers, or send it back
 *  with comments for a full fix. */
export function FixDecisionCards({ d, decision, onChanged }: {
  d: IssueDetail; decision: FixDecision; onChanged: () => void;
}) {
  const { dryRun, pushToast } = useExec();
  const { pr } = decision;
  const [partial, setPartial] = useState(decision.partial);
  const [text, setText] = useState(() => sendBackDraft(d));
  const [busy, setBusy] = useState(false);

  const act = (call: () => Promise<unknown>, done: string) => {
    setBusy(true);
    call()
      .then(() => { pushToast(done, "green"); onChanged(); })
      .catch((e: Error) => pushToast("Couldn't do that", "red", { detail: e.message }))
      .finally(() => setBusy(false));
  };

  return (
    <div className="fix-decision">
      <div className="fix-decision-card">
        <h4>Approve as-is</h4>
        <div className="small">
          <b>Means:</b> you agree with the fixer that #{pr} is the fix. It leaves your queue and
          waits in the maintainers' normal PR queue.
        </div>
        <div className="muted small">
          It comes back to you only if the fixer fails at something new: its revisions run out,
          or a maintainer's requested change doesn't land. CI that fails outside the change
          doesn't bring it back.
        </div>
        <label className="small fix-decision-check">
          <input type="checkbox" checked={partial} onChange={(e) => setPartial(e.target.checked)} />
          It fixes only part of #{d.number}: change <code>Fixes #{d.number}</code> to
          {" "}<code>Addresses part of #{d.number}</code>, so merging leaves the issue open
        </label>
        <div className="row-actions">
          <button className="btn-primary sm" disabled={busy}
            title="Recorded here only; nothing is posted upstream except the description edit for a partial fix"
            onClick={() => act(() => api.issueFixApprove(d.number, partial),
                               `Approved #${pr}; it's with the maintainers`)}>
            Approve #{pr}
          </button>
        </div>
      </div>
      <div className="fix-decision-or">or</div>
      <div className="fix-decision-card">
        <h4>Request full fix</h4>
        <div className="small">
          <b>Means:</b> you agree with the reviewer that #{pr} isn't enough. The agent revises it
          with your comments and pushes the revision onto #{pr}
          {dryRun ? " (in dry run nothing is pushed)" : ""}.
        </div>
        <div className="muted small">
          If the revision still leaves part of the issue, or ends without a fix, it comes back
          to you.
        </div>
        <textarea className="fix-goal" rows={6} value={text} onChange={(e) => setText(e.target.value)}
                  aria-label={`Your comments for the revision of #${pr}`} />
        <div className="row-actions">
          <button className="btn-secondary sm" disabled={busy || !text.trim()}
            onClick={() => act(() => api.issueFix(d.number, { action: "send-back", guidance: text,
                                                             dry_run: dryRun }),
                               `Sent back: the revision goes onto #${pr}`)}>
            Send back for re-fix{dryRun ? " (dry run)" : ""}
          </button>
        </div>
      </div>
    </div>
  );
}
