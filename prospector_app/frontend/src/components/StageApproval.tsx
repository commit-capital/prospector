import { useState } from "react";
import { api, type StageApproval } from "../api";
import { useExec } from "../ExecContext";

const WHAT: Record<string, string> = {
  design: "design review past its findings",
  security: "YELLOW security verdict",
  verify: "escalated verification",
};

/** Approve a check's stage at the head its verdict was taken at, with a
 *  required reason (pipeline/approvals.py): the check passes and the PR moves
 *  on until a new verdict replaces it. */
export function ApproveButton({ pr, stage, head, onDone }: {
  pr: number; stage: string; head: string; onDone: () => Promise<void> | void;
}) {
  const { pushToast } = useExec();
  const [busy, setBusy] = useState(false);
  const approve = async () => {
    const reason = window.prompt(`Approve #${pr}'s ${WHAT[stage] ?? stage} at ${head.slice(0, 7)}? `
      + "Say why — it's logged to Activity. A new verdict replaces the approval.");
    if (!reason?.trim()) return;
    setBusy(true);
    try {
      await api.approveStage(pr, stage, head, reason.trim());
      pushToast(`#${pr} · ${stage} approved`, "green");
      await onDone();
    } catch (e) {
      pushToast(`#${pr} · couldn't approve ${stage}`, "red", { detail: String(e) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <button className="btn-secondary sm" disabled={busy} onClick={() => void approve()}
      title={`You've weighed the ${WHAT[stage] ?? stage} and accept it: the check passes at this commit and the PR moves on. A new verdict is judged again.`}>
      {busy ? "Approving…" : "✓ Approve"}
    </button>
  );
}

/** Who approved a check's stage, when, and why. */
export function ApprovalNote({ approval }: { approval: StageApproval }) {
  return (
    <div className="small muted">
      ✅ Approved by {approval.by ?? "an operator"} {approval.at.slice(0, 10)}: {approval.reason}
    </div>
  );
}
