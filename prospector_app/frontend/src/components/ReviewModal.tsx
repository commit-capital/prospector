import { useState } from "react";
import { api, type ExecResult } from "../api";
import { useExec } from "../ExecContext";

/** Small composer for an inline review comment on a diff line. */
export function LineCommentBox({ pr, file, line, onClose }: { pr: number; file: string; line: number; onClose: () => void }) {
  const { botLogin, dryRun, reportResult } = useExec();
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ExecResult | null>(null);

  const submit = async () => {
    if (!body.trim()) return;
    if (!dryRun && !window.confirm(`Post an inline comment on ${file}:${line} as ${botLogin}?`)) return;
    setBusy(true);
    const r = await api.commentLine(pr, file, line, body, dryRun);
    setResult(r);
    setBusy(false);
    reportResult(r);
  };

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head"><b>Comment on {file.split("/").pop()}:{line}</b><button className="flyout-btn" onClick={onClose}>✕</button></div>
        <textarea className="modal-text" rows={3} placeholder="Leave a line comment" value={body} onChange={(e) => setBody(e.target.value)} autoFocus />
        {result && <div className={`callout ${result.status === "error" ? "err" : ""}`}>{result.status === "dry-run" ? "🔎 " : result.status === "executed" ? "✅ " : "⚠ "}{result.detail}</div>}
        <div className="modal-foot">
          <span className="muted small">{dryRun ? "Dry-run" : `LIVE — as ${botLogin}`}</span>
          <button className="btn-secondary sm" onClick={onClose}>Cancel</button>
          <button className={`btn-primary sm ${!dryRun ? "btn-live" : ""}`} disabled={busy || !body.trim()} onClick={submit}>
            {busy ? "…" : "Comment"}
          </button>
        </div>
      </div>
    </div>
  );
}
