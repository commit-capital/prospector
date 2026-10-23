import { useCallback, useEffect, useState } from "react";
import { api, type ReviewVerdict } from "../api";
import { useExec } from "../ExecContext";
import { Collapsible } from "./Collapsible";

/** Each reviewer's verdicts, least to most severe, without the failure value
 *  that ends each list in pipeline/verdict_log.py (a run that never judged
 *  takes no label). */
const JUDGING: Record<string, string[]> = {
  design: ["pass", "fail"],
  security: ["GREEN", "YELLOW", "RED"],
  "fix-review": ["safe", "unsafe"],
  "trust-boundary": ["pass", "hold"],
  intake: ["clear", "suspicious", "malicious"],
  fixability: ["go", "ask", "skip"],
};

function when(iso: string): string {
  return iso.replace("T", " ").slice(0, 16);
}

/** The recorded runs of Prospector's own reviewers on one PR or issue, each
 *  with the operator's label or a form to set one: what the reviewer should
 *  have said, and why. Labels feed the reviewer eval ratchet. */
export function ReviewerVerdicts({ kind, subject }: { kind: "pr" | "issue"; subject: number }) {
  const [rows, setRows] = useState<ReviewVerdict[] | null>(null);
  const load = useCallback(() => {
    api.verdicts(kind, subject).then((r) => setRows(r.verdicts)).catch(() => setRows([]));
  }, [kind, subject]);
  useEffect(load, [load]);
  if (!rows || rows.length === 0) return null;
  const labeled = rows.filter((r) => r.label).length;
  return (
    <Collapsible summary={<>Reviewer verdicts · {rows.length} run{rows.length === 1 ? "" : "s"}
      {labeled > 0 && <span className="muted"> · {labeled} labeled</span>}</>}>
      {rows.map((r) => <VerdictRow key={r.id} row={r} onSaved={load} />)}
    </Collapsible>
  );
}

function VerdictRow({ row, onSaved }: { row: ReviewVerdict; onSaved: () => void }) {
  const { pushToast } = useExec();
  const choices = JUDGING[row.reviewer] ?? [];
  const [expected, setExpected] = useState(row.label?.expected ?? row.verdict);
  const [note, setNote] = useState(row.label?.note ?? "");
  const [busy, setBusy] = useState(false);
  const judged = choices.includes(row.verdict);
  const save = () => {
    setBusy(true);
    // Marks and the source are set through the API; a save here keeps them.
    api.labelVerdict(row.id, { expected, note, findings: row.label?.findings,
      source: row.label?.source })
      .then(() => { pushToast("Label saved", "green"); onSaved(); })
      .catch((e: Error) => pushToast("Couldn't save the label", "red", { detail: e.message }))
      .finally(() => setBusy(false));
  };
  return (
    <div className="panel">
      <div>
        <b>{row.reviewer}</b> said <b>{row.verdict}</b>
        <span className="muted small"> · {row.model} · {when(row.at)}
          {row.head_sha && <> · {row.head_sha.slice(0, 7)}</>}</span>
      </div>
      {row.label && (
        <div className="small">
          {row.label.expected === row.verdict ? "Right" : <>Should have been <b>{row.label.expected}</b></>}
          <span className="muted"> · {row.label.by}, {when(row.label.at)}</span>
          {row.label.note && <div className="muted">“{row.label.note}”</div>}
        </div>
      )}
      {judged ? (
        <div className="small">
          <label>
            Should have said{" "}
            <select value={expected} onChange={(e) => setExpected(e.target.value)} disabled={busy}>
              {choices.map((c) => <option key={c} value={c}>{c === row.verdict ? `${c} (right)` : c}</option>)}
            </select>
          </label>
          <textarea value={note} onChange={(e) => setNote(e.target.value)} disabled={busy}
            placeholder="Why (optional): what it missed or got wrong" rows={2} style={{ width: "100%" }} />
          <button onClick={save} disabled={busy}>{row.label ? "Update label" : "Save label"}</button>
        </div>
      ) : (
        <div className="muted small">This run never judged, so it takes no label.</div>
      )}
    </div>
  );
}
