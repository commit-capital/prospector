import type { ReactNode } from "react";
import { chipTone } from "./bulkSummary";

// The backdrop ignores clicks while a run is in flight: losing the dialog
// mid-run leaves no way to tell whether it is still going.
export function BulkDialogFrame({ title, running, onClose, children }:
  { title: ReactNode; running: boolean; onClose: () => void; children: ReactNode }) {
  return (
    <div className="modal-backdrop" onClick={() => { if (!running) onClose(); }}>
      <div className="modal bulk-confirm" onClick={(e) => e.stopPropagation()}>
        <h3>{title}</h3>
        {children}
      </div>
    </div>
  );
}

export function BulkStatusChip({ status, detail }: { status: string; detail?: string | null }) {
  return <span className={`chip chip-${chipTone(status)}`} title={detail ?? undefined}>{status}</span>;
}
