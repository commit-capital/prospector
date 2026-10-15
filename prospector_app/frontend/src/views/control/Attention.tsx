import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api, type ControlOverview, type FilterSpec, type OverviewNeed } from "../../api";
import { ago } from "./format";
import { isMuted, mute, readMutes, unmuteAll } from "./mute";

function NeedRow({ need, onChanged, onReviewParked, onMute }: {
  need: OverviewNeed; onChanged: () => void; onReviewParked: () => void; onMute: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true); setErr(null);
    try { await fn(); onChanged(); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };
  const action =
    need.kind === "trip" && need.host && need.lane
      ? <button className="btn-sm" disabled={busy} onClick={() => run(() => api.workerHealthResume(need.host!, need.lane!))}>{busy ? "Resuming…" : "Resume"}</button>
      : need.kind === "offline" && need.host
        ? <button className="btn-sm" disabled={busy} onClick={() => run(() => api.standDown(need.host!))}
            title="Mark this machine off on purpose. It reads as working again the next time one of its workers beats.">
            {busy ? "…" : "Stand down"}</button>
        : need.kind === "parked"
          ? <button className="btn-sm" onClick={onReviewParked}>Review</button>
          : null;
  const hasDetail = need.detail != null || need.groups.length > 0;
  return (
    <div className="attn-row">
      <div className="attn-head">
        <span>{need.title}</span>
        <span className="muted small">{need.at ? ago(need.at) : ""}</span>
        {hasDetail && <button className="linkish small" aria-expanded={open} onClick={() => setOpen((o) => !o)}>{open ? "less" : "details"}</button>}
        <span style={{ flex: 1 }} />
        {err && <span className="activity-bad small">{err}</span>}
        {action}
        <button className="linkish small" onClick={onMute} title="Hide this for 24 hours in this browser">mute</button>
      </div>
      {open && (
        <div className="attn-detail muted small">
          {need.detail && <div style={{ whiteSpace: "pre-wrap" }}>{need.detail}</div>}
          {need.groups.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
              {need.groups.map((g) => {
                const spec: FilterSpec = { numbers: g.prs, state: "all" };
                return (
                  <Link key={`${g.lane}-${g.reason}`} className="chip chip-red sm"
                    to={`/explore?spec=${encodeURIComponent(JSON.stringify(spec))}`}>
                    {g.lane} · {g.reason.length > 60 ? `${g.reason.slice(0, 60)}…` : g.reason} ×{g.prs.length}
                  </Link>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** The problems a person must act on. Absent when there are none. */
export function Attention({ overview, onChanged, onReviewParked }: {
  overview: ControlOverview; onChanged: () => void; onReviewParked: () => void;
}) {
  const [mutes, setMutes] = useState<Record<string, number>>(() => readMutes());
  // Mutes expire against a clock that ticks each minute.
  const [now, setNow] = useState<number>(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(t);
  }, []);
  const shown = overview.needs.filter((n) => !isMuted(n.key, mutes, now));
  const muted = overview.needs.length - shown.length;
  if (shown.length === 0 && muted === 0) return null;
  const unmute = <button className="linkish small" onClick={() => { unmuteAll(); setMutes(readMutes()); }}>unmute</button>;
  if (shown.length === 0) {
    return <div className="status-line">Nothing needs you · {muted} muted {unmute}</div>;
  }
  return (
    <section className="cpanel tone-danger" aria-label="Needs you">
      <div className="cpanel-head">
        <h3 className="cpanel-title">Needs you · {shown.length}</h3>
        {muted > 0 && <span className="cpanel-meta">{muted} muted {unmute}</span>}
      </div>
      <div className="cpanel-body">
        {shown.map((n) => (
          <NeedRow key={n.key} need={n} onChanged={onChanged} onReviewParked={onReviewParked}
            onMute={() => { const at = Date.now(); mute(n.key, at); setMutes(readMutes()); setNow(at); }} />
        ))}
      </div>
    </section>
  );
}
