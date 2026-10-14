import { Fragment, useEffect, useState } from "react";
import type { Autohunt, ControlOverview, FixQueue, OverviewLane, VerifyQueue } from "../../api";
import { FlyoutIssueLink } from "../../components/FlyoutIssueLink";
import { PRLink } from "../../components/PRLink";
import { ago, elapsed } from "./format";
import { cellLabel, nextText } from "./overviewText";
import { FixTable, VerifyTable } from "./Queues";

const TONE: Record<string, string> = {
  on: "chip-green", off: "chip-muted", tripped: "chip-red", "ai-paused": "chip-amber",
  "stood-down": "chip-muted", offline: "chip-red",
};

function NowCell({ lane, now }: { lane: OverviewLane; now: number }) {
  if (lane.now.length === 0) return <span className="muted">idle</span>;
  return (
    <>
      {lane.now.map((n, i) => (
        <div key={i}>
          {n.pr != null ? <PRLink n={n.pr} /> : n.issue != null ? <FlyoutIssueLink n={n.issue} /> : null}
          {n.step && <span className="muted"> · {n.step}</span>}
          {n.started_at && <span className="muted"> · {elapsed(n.started_at, now)}</span>}
          {n.host && <span className="muted small"> · {n.host}</span>}
        </div>
      ))}
    </>
  );
}

function Expanded({ lane, overview, verifyQueue, fixQueue, hunt, onFixChanged }: {
  lane: OverviewLane; overview: ControlOverview; verifyQueue: VerifyQueue | null; fixQueue: FixQueue | null;
  hunt: Autohunt | null; onFixChanged: () => Promise<unknown>;
}) {
  if (lane.lane === "verify" && verifyQueue) return <VerifyTable queue={verifyQueue} hunt={hunt} />;
  if (lane.lane === "autofix" && fixQueue) return <FixTable queue={fixQueue} onChanged={onFixChanged} />;
  const recent = overview.timeline.filter((t) => t.when === "past" && t.lane === lane.lane).slice(0, 10);
  return (
    <div className="small">
      {lane.queue.length > 0 && (
        <ol className="lane-queue">
          {lane.queue.map((q, i) => (
            <li key={i}>{q.pr != null ? <PRLink n={q.pr} /> : <FlyoutIssueLink n={q.issue!} />}
              {q.title && <span className="muted"> {q.title.slice(0, 70)}</span>}
              <span className="muted"> · {q.source ?? "auto"} · {ago(q.queued_at)}</span></li>
          ))}
        </ol>
      )}
      {recent.length === 0 ? <div className="muted">Nothing ended in the past day.</div>
        : recent.map((t, i) => (
          <div key={i} className={t.bad ? "activity-bad" : undefined}>
            <span className="muted">{new Date(t.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span> {t.text}
            {t.host && <span className="muted"> · {t.host}</span>}
          </div>
        ))}
    </div>
  );
}

/** One row per lane: its state on each machine, what it runs now and next,
 *  and its past day. A row opens its queue and recent endings. */
export function Lanes({ overview, open, onToggle, verifyQueue, fixQueue, hunt, onFixChanged }: {
  overview: ControlOverview; open: string | null; onToggle: (lane: string) => void;
  verifyQueue: VerifyQueue | null; fixQueue: FixQueue | null; hunt: Autohunt | null;
  onFixChanged: () => Promise<unknown>;
}) {
  const [now, setNow] = useState<number>(() => Date.now());
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
        <thead><tr><th>Lane</th><th>State</th><th>Now</th><th>Next</th><th title="Runs ended in the past 24 hours">24h</th></tr></thead>
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
                <td><NowCell lane={l} now={now} /></td>
                <td className="muted">{nextText(l, overview.ai_paused?.retry_at ?? null)}
                  {l.parked > 0 && <b> · {l.parked} parked</b>}</td>
                <td>{l.ended_24h}{l.faults_24h > 0 && <span className="activity-bad"> ({l.faults_24h} failed)</span>}</td>
              </tr>
              {open === l.lane && (
                <tr><td colSpan={5}><Expanded lane={l} overview={overview} verifyQueue={verifyQueue}
                  fixQueue={fixQueue} hunt={hunt} onFixChanged={onFixChanged} /></td></tr>
              )}
            </Fragment>
          ))}
          <tr>
            <td className="muted">Background</td>
            <td colSpan={4} className="muted small">
              {overview.loops.length === 0 ? "no machine runs the periodic loops"
                : overview.loops.map((lp) => (
                  <span key={`${lp.host}-${lp.loop}`} className="loop-chip">
                    {lp.label}{many ? ` (${lp.host.split(/[.-]/)[0]})` : ""}: {lp.next_at
                      ? `next ${new Date(lp.next_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
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
