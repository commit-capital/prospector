import { Fragment, useEffect, useState } from "react";
import { api, type ControlOverview, type FixQueue, type OverviewLane, type OverviewQueueItem } from "../../api";
import { FlyoutIssueLink } from "../../components/FlyoutIssueLink";
import { PRLink } from "../../components/PRLink";
import { ago, elapsed } from "./format";
import { canReorder, cellLabel, healthLane, nextText } from "./overviewText";
import { LogTail } from "./LogTail";
import { FixTable } from "./Queues";

const TONE: Record<string, string> = {
  on: "chip-green", off: "chip-muted", paused: "chip-amber", tripped: "chip-red",
  "ai-paused": "chip-amber", "stood-down": "chip-muted", offline: "chip-red",
};

// The queue each lane's Move to top and Remove act on.
const QUEUE_KIND: Record<string, "verify" | "fix" | "issue-fix"> = {
  verify: "verify", autofix: "fix", "issue-fix": "issue-fix",
};

function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** Run one control action, then reload; its error shows beside the controls. */
function useAction(onChanged: () => void): [(fn: () => Promise<unknown>) => void, boolean, string | null] {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const run = (fn: () => Promise<unknown>) => {
    setBusy(true); setErr(null);
    fn().then(onChanged, (e: unknown) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false));
  };
  return [run, busy, err];
}

function NowCell({ lane, now, local, logOpen, onLog }: {
  lane: OverviewLane; now: number; local: string; logOpen: boolean; onLog: () => void;
}) {
  if (lane.now.length === 0) return <span className="muted">idle</span>;
  return (
    <>
      {lane.now.map((n, i) => (
        <div key={i}>
          {n.pr != null ? <PRLink n={n.pr} /> : n.issue != null ? <FlyoutIssueLink n={n.issue} /> : null}
          {n.step && <span className="muted"> · {n.step}</span>}
          {n.started_at && <span className="muted"> · {elapsed(n.started_at, now)}</span>}
          {n.host && <span className="muted small"> · {n.host}</span>}
          {n.host === local && (
            <button className="linkish small" aria-expanded={logOpen}
              onClick={(e) => { e.stopPropagation(); onLog(); }}
              title="Follow this machine's worker log">{logOpen ? " log ▴" : " log ▾"}</button>
          )}
        </div>
      ))}
    </>
  );
}

/** Pause or resume this lane on the machine serving the app. */
function PauseControl({ lane, local, onChanged }: { lane: OverviewLane; local: string; onChanged: () => void }) {
  const [run, busy, err] = useAction(onChanged);
  const mine = lane.cells.find((c) => c.host === local);
  if (!mine || !["on", "tripped", "ai-paused", "paused"].includes(mine.state)) return null;
  const paused = mine.state === "paused";
  return (
    <span onClick={(e) => e.stopPropagation()}>
      <button className="btn-sm" disabled={busy}
        title={paused ? `Let ${local} take ${lane.label} work again`
          : `Stop ${local} taking new ${lane.label} work; what it runs now finishes`}
        onClick={() => run(() => (paused ? api.resumeLane : api.pauseLane)(healthLane(lane.lane), local))}>
        {busy ? "…" : paused ? "Resume" : "Pause"}
      </button>
      {err && <span className="activity-bad small"> {err}</span>}
    </span>
  );
}

function QueueList({ lane, onChanged }: { lane: OverviewLane; onChanged: () => void }) {
  const [run, busy, err] = useAction(onChanged);
  const kind = QUEUE_KIND[lane.lane];
  const remove = (q: OverviewQueueItem) =>
    kind === "verify" ? api.dequeueVerify(q.pr!) : kind === "fix" ? api.dequeueFix(q.pr!) : api.cancelIssueFix(q.issue!);
  return (
    <div className="lane-section">
      <div className="lane-section-title">Queue, in the order it runs</div>
      <ol className="lane-queue">
        {lane.queue.map((q, i) => {
          const n = (q.pr ?? q.issue)!;
          return (
            <li key={n}>
              {q.pr != null ? <PRLink n={q.pr} /> : <FlyoutIssueLink n={n} />}
              {q.title && <span className="muted"> {q.title.slice(0, 70)}</span>}
              <span className="muted"> · {q.source ?? "auto"} · {ago(q.queued_at)}{q.priority ? " · moved up" : ""}</span>
              {q.status === "approved" && <span className="muted"> · approved, pushing next</span>}
              {canReorder(lane.lane, q.status) && i > 0 && (
                <button className="linkish small" disabled={busy} onClick={() => run(() => api.queueTop(kind, n))}
                  title="Run this next">move to top</button>
              )}
              {canReorder(lane.lane, q.status) && (
                <button className="linkish small" disabled={busy} onClick={() => run(() => remove(q))}
                  title="Take this out of the queue">remove</button>
              )}
            </li>
          );
        })}
      </ol>
      {err && <div className="activity-bad small">{err}</div>}
    </div>
  );
}

function Expanded({ lane, overview, fixQueue, onFixChanged, onChanged }: {
  lane: OverviewLane; overview: ControlOverview; fixQueue: FixQueue | null;
  onFixChanged: () => Promise<unknown>; onChanged: () => void;
}) {
  const [run, busy] = useAction(onChanged);
  const pausedOn = lane.cells.filter((c) => c.state === "paused");
  const recent = overview.timeline.filter((t) => t.when === "past" && t.lane === lane.lane).slice(0, 10);
  return (
    <div className="small lane-expanded">
      {pausedOn.length > 0 && (
        <div className="lane-section">
          {pausedOn.map((c) => (
            <div key={c.host}>Paused on <b>{c.host}</b>{" "}
              <button className="linkish small" disabled={busy}
                onClick={() => run(() => api.resumeLane(healthLane(lane.lane), c.host))}>resume</button>
            </div>
          ))}
        </div>
      )}
      {lane.queue.length > 0 && QUEUE_KIND[lane.lane] && <QueueList lane={lane} onChanged={onChanged} />}
      {lane.picks.length > 0 && (
        <div className="lane-section">
          <div className="lane-section-title">The hunter's next picks</div>
          <ol className="lane-queue">
            {lane.picks.map((p) => (
              <li key={p.pr}><PRLink n={p.pr} />
                {p.action && <span className="chip chip-muted sm">{p.action}</span>}
                {p.title && <span className="muted"> {p.title.slice(0, 70)}</span>}
              </li>
            ))}
          </ol>
        </div>
      )}
      {lane.lane === "autofix" && fixQueue && (
        <div className="lane-section"><FixTable queue={fixQueue} onChanged={onFixChanged} /></div>
      )}
      <div className="lane-section">
        <div className="lane-section-title">Past day</div>
        {recent.length === 0 ? <div className="muted">Nothing ended in the past day.</div>
          : recent.map((t, i) => (
            <div key={i} className={t.bad ? "activity-bad" : undefined}>
              <span className="muted">{clock(t.at)}</span> {t.text}
              {t.host && <span className="muted"> · {t.host}</span>}
            </div>
          ))}
      </div>
    </div>
  );
}

/** One row per lane: its state on each machine, what it runs now and next,
 *  its past day, and a pause for this machine. A row opens its pauses, its
 *  queue with Move to top, the hunter's next picks, and recent endings; an
 *  item this machine runs opens its worker log. */
export function Lanes({ overview, open, onToggle, fixQueue, onFixChanged, onChanged }: {
  overview: ControlOverview; open: string | null; onToggle: (lane: string) => void;
  fixQueue: FixQueue | null; onFixChanged: () => Promise<unknown>; onChanged: () => void;
}) {
  const [now, setNow] = useState<number>(() => Date.now());
  const [logFor, setLogFor] = useState<string | null>(null);
  const live = overview.lanes.some((l) => l.now.length > 0);
  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(t);
  }, [live]);
  const many = overview.machines.length > 1;
  return (
    <section className="cpanel" aria-label="Lanes">
      <div className="cpanel-head"><h3 className="cpanel-title">Lanes</h3></div>
      <table className="grid compact lanes">
        <thead><tr><th>Lane</th><th>State</th><th>Now</th><th>Next</th><th title="Runs ended in the past 24 hours">24h</th><th /></tr></thead>
        <tbody>
          {overview.lanes.map((l) => (
            <Fragment key={l.lane}>
              <tr className={`lane-row${open === l.lane ? " open" : ""}`} onClick={() => onToggle(l.lane)}
                aria-expanded={open === l.lane}>
                <td><span className={`caret ${open === l.lane ? "open" : ""}`}>▸</span> {l.label}</td>
                <td>{l.cells.length === 0 ? <span className="chip chip-muted sm">off</span> : l.cells.map((c) => (
                  <span key={c.host} className={`chip sm ${TONE[c.state]}`} title={c.host}>
                    {many ? `${c.host.split(/[.-]/)[0]}: ` : ""}{cellLabel(c.state)}
                  </span>
                ))}</td>
                <td><NowCell lane={l} now={now} local={overview.local} logOpen={logFor === l.lane}
                  onLog={() => setLogFor((f) => (f === l.lane ? null : l.lane))} /></td>
                <td className="muted">{nextText(l, overview.ai_paused?.retry_at ?? null)}
                  {l.parked > 0 && <b> · {l.parked} parked</b>}</td>
                <td>{l.ended_24h}{l.faults_24h > 0 && <span className="activity-bad"> ({l.faults_24h} failed)</span>}</td>
                <td><PauseControl lane={l} local={overview.local} onChanged={onChanged} /></td>
              </tr>
              {logFor === l.lane && l.now.some((n) => n.host === overview.local) && (
                <tr><td colSpan={6}><LogTail /></td></tr>
              )}
              {open === l.lane && (
                <tr><td colSpan={6}><Expanded lane={l} overview={overview} fixQueue={fixQueue}
                  onFixChanged={onFixChanged} onChanged={onChanged} /></td></tr>
              )}
            </Fragment>
          ))}
          <tr>
            <td className="muted">Background</td>
            <td colSpan={5} className="muted small">
              {overview.loops.length === 0 ? "no machine runs the periodic loops"
                : overview.loops.map((lp) => (
                  <span key={`${lp.host}-${lp.loop}`} className="loop-chip">
                    {lp.label}{many ? ` (${lp.host.split(/[.-]/)[0]})` : ""}: {lp.next_at
                      ? `next ${clock(lp.next_at)}`
                      : `last ${ago(lp.last_at)}`}
                  </span>
                ))}
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  );
}
