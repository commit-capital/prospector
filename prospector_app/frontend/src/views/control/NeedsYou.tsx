import { useState, type ReactNode } from "react";
import { Link } from "react-router";
import { api, type ActivityView, type Autohunt, type CapacityState, type FilterSpec, type VerifyBaseHost, type WorkerHealthHost } from "../../api";
import { groupNeedsAttention } from "../needsAttention";
import { Panel } from "./Panel";
import { ago, localTime } from "./format";

/** One problem: a headline, an optional action, and detail behind an expander. */
function Line({ title, meta, action, children }: {
  title: ReactNode;
  meta?: ReactNode;
  action?: ReactNode;
  children?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="cpanel-row">
      <div className="cpanel-row-head">
        <b>{title}</b>
        {meta && <span className="muted small">{meta}</span>}
        {children && (
          <button className="linkish small" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
            {open ? "less ▴" : "details ▾"}
          </button>
        )}
        <span style={{ flex: 1 }} />
        {action}
      </div>
      {open && children && <div className="cpanel-row-detail">{children}</div>}
    </div>
  );
}

/** A tripped lane: it stopped picking work because the machine, not the PRs,
 *  kept failing. Resume reopens it before its own retest does. */
function TrippedLane({ host, lane, onResume }: { host: WorkerHealthHost; lane: string; onResume: () => void }) {
  const h = host.lanes[lane] ?? {};
  const t = h.tripped ?? {};
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const resume = async () => {
    setBusy(true); setErr(null);
    try { await api.workerHealthResume(host.host, lane); onResume(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  return (
    <Line title={`${lane} lane paused on ${host.host}`}
      meta={<>tripped {ago(t.at)}{t.kind ? ` · ${t.kind}` : ""}{err && <span className="activity-bad"> · {err}</span>}</>}
      action={<button className="btn-sm" disabled={busy} onClick={resume}>{busy ? "Resuming…" : "Resume"}</button>}>
      <div>{t.reason}</div>
      {h.remedy && <div style={{ marginTop: 4 }}><b>To fix:</b> {h.remedy}</div>}
      {h.retest && !h.retest.ok && (
        <div className="muted" style={{ marginTop: 4 }}>last self-test {ago(h.retest.at)}: {h.retest.detail}</div>
      )}
    </Line>
  );
}

function pinTrouble(b: VerifyBaseHost): string | null {
  if (b.refresh_failures > 0) {
    return `refresh failing · ${b.refresh_failures} ${b.refresh_failures === 1 ? "attempt" : "attempts"}`;
  }
  return b.stale ? "not tracking the default branch" : null;
}

/** Everything that needs a person: tripped lanes, offline workers, failing
 *  base pins, paused background AI, and failed hunter runs. One healthy line
 *  when there is none. */
export function NeedsYou({ hunt, activity, capacity, onResume }: {
  hunt: Autohunt | null;
  activity: ActivityView | null;
  capacity: CapacityState | null;
  onResume: () => void;
}) {
  const lines: ReactNode[] = [];
  for (const h of hunt?.status.health.hosts ?? []) {
    for (const lane of h.tripped) {
      lines.push(<TrippedLane key={`trip-${h.host}-${lane}`} host={h} lane={lane} onResume={onResume} />);
    }
  }
  for (const m of activity?.machines ?? []) {
    if (!m.stalled) continue;
    lines.push(
      <Line key={`offline-${m.host}`} title={`${m.host} worker offline`}
        meta={m.offline_since ? `last heartbeat ${ago(m.offline_since)}` : "no heartbeat"}>
        Its workers have stopped heartbeating, so nothing it claimed is progressing. Restart the
        worker on {m.host}; a run it held is reclaimed by another machine after an hour.
      </Line>);
  }
  const pins = (hunt?.status.base.hosts ?? []).filter((b) => pinTrouble(b) != null);
  if (pins.length > 0) {
    lines.push(
      <Line key="pins" title="Base pin refresh failing"
        meta={pins.length === 1 ? pins[0].host : `${pins.length} machines`}>
        {pins.map((b) => (
          <div key={b.host} title={b.refresh_error ?? undefined}>
            <b>{b.host}</b> · {pinTrouble(b)} · base {b.base_sha} · pinned {ago(b.pinned_at)}
            {b.refresh_error && <div className="muted mono small">{b.refresh_error.slice(0, 300)}</div>}
          </div>
        ))}
      </Line>);
  }
  for (const a of capacity?.accounts ?? []) {
    if (a.decision.allowed) continue;
    lines.push(
      <Line key={`ai-${a.key}`} title="Background AI paused"
        meta={<>{a.label} · {a.decision.reason}{a.decision.retry_at && ` · resumes ~${localTime(a.decision.retry_at)}`}</>} />);
  }
  const attention = hunt
    ? groupNeedsAttention(hunt.status.security_failed,
        hunt.status.security_failed_reasons ?? {}, hunt.status.verify_failed)
    : [];
  if (attention.length > 0) {
    const runs = attention.reduce((n, g) => n + g.prs.length, 0);
    lines.push(
      <Line key="failed" title={`${runs} failed hunter run${runs === 1 ? "" : "s"}`}
        meta={`across ${attention.length} reason${attention.length === 1 ? "" : "s"}`}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          {attention.map((g) => {
            const spec: FilterSpec = { numbers: g.prs, state: "all" };
            return (
              <Link key={`${g.lane}-${g.reason}`}
                to={`/explore?spec=${encodeURIComponent(JSON.stringify(spec))}`}
                className="chip chip-red sm"
                title={`${g.prs.length} failed ${g.lane} run${g.prs.length === 1 ? "" : "s"}: ${g.reason} — open them in PR Explorer`}>
                {g.lane === "security" ? "🛡" : "🧪"} {g.reason.length > 60 ? `${g.reason.slice(0, 60)}…` : g.reason} ×{g.prs.length}
              </Link>
            );
          })}
        </div>
      </Line>);
  }
  return (
    <Panel id="needs-you" title="Needs you" tone={lines.length > 0 ? "danger" : undefined}
      meta={lines.length > 0 ? String(lines.length) : undefined}>
      {lines.length > 0 ? lines
        : hunt != null ? <div className="cpanel-ok">✓ All lanes healthy</div>
        : <div className="muted small">Loading…</div>}
    </Panel>
  );
}
