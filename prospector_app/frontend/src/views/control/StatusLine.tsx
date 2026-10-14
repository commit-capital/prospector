import { useState } from "react";
import { api, type ControlOverview } from "../../api";
import { statusLine } from "./overviewText";

/** One sentence for every machine and the AI hold, with Bring back on a
 *  machine an operator stood down. */
export function StatusLine({ overview, onChanged }: { overview: ControlOverview; onChanged: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const off = overview.machines.filter((m) => m.stood_down);
  const up = async (host: string) => {
    setBusy(host);
    try { await api.standUp(host); onChanged(); } finally { setBusy(null); }
  };
  return (
    <div className="status-line">
      <span>{statusLine(overview)}</span>
      {off.map((m) => (
        <button key={m.host} className="linkish small" disabled={busy === m.host} onClick={() => up(m.host)}
          title={m.stood_down_note ? `Stood down: ${m.stood_down_note}` : `Clear the stand-down mark on ${m.host}`}>
          {busy === m.host ? "…" : `bring ${m.host} back`}
        </button>
      ))}
    </div>
  );
}
