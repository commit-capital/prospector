import { useEffect, useState } from "react";
import { api, type NextStep, type PRStatus } from "../api";
import { useExec } from "../ExecContext";
import { useAgentPane } from "./AgentPane";
import { stateLabel, stateTone } from "./prStatus";
import { actionsFor } from "./prStatusActions";

const OWNER_LINE: Record<string, string> = {
  worker: "Prospector's move · nothing for you to do",
  author: "The author's move",
  maintainers: "The maintainers' move",
  you: "Your move",
};

const LANE_NAME: Record<NextStep["lane"], string> = {
  security: "security review", verify: "test run", design: "design review", fix: "autofix",
};

function eta(hours: number | null): string {
  if (hours === null) return "when is unknown";
  if (hours < 1) return "within the hour";
  return hours < 48 ? `about ${Math.round(hours)} hours` : `about ${Math.round(hours / 24)} days`;
}

/** The top of an open PR's page: its state, the one sentence why, the path
 *  through the stages, and the actions that fit (pr_status / automation). The
 *  merge, close and checks actions live on the page; the block points at them. */
export function PRStatusBlock({ prNum, reload, onRun, onScrollTo }: {
  prNum: number;
  reload: number;
  onRun: (lane: NextStep["lane"]) => void;
  onScrollTo: (id: "pr-actions" | "pr-checks") => void;
}) {
  const { dryRun, pushToast } = useExec();
  const { askFresh } = useAgentPane();
  const [status, setStatus] = useState<PRStatus | null>(null);
  const [next, setNext] = useState<NextStep | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api.prStatus(prNum).then((s) => {
      if (!live) return;
      setStatus(s);
      setNext(null);
      if (s.standing.state === "moving") {
        api.prNextStep(prNum).then((n) => { if (live) setNext(n); }).catch(() => {});
      }
    }).catch(() => { if (live) setStatus(null); });
    return () => { live = false; };
  }, [prNum, reload]);

  if (!status) return null;
  const { standing, stages, tried, issue, seed } = status;
  const ours = issue !== null;

  const act = async (what: string, call: () => Promise<unknown>) => {
    setBusy(true);
    try { await call(); pushToast(`#${prNum} · ${what}`, "green"); }
    catch (e) { pushToast(`#${prNum} · ${what} failed`, "red", { detail: String(e) }); }
    finally { setBusy(false); }
  };
  const reattempt = () => {
    const notes = window.prompt("What should the next attempt do differently?");
    if (!notes) return;
    void act("re-attempt queued", () => (ours && issue !== null
      ? api.issueFix(issue, { action: "send-back", guidance: notes })
      : api.queueFix(prNum, "fix", notes)));
  };
  const accept = () => {
    if (ours && issue !== null) void act("approved for the maintainers", () => api.issueFixApprove(issue, false));
    else onScrollTo("pr-actions");
  };
  const quickClick = () => {
    if (standing.bucket === "approve-parked") {
      void act(dryRun ? "parked change approved (dry run)" : "parked change approved",
        () => api.approveFix(prNum, dryRun));
    } else onScrollTo(standing.bucket === "malicious" ? "pr-actions" : "pr-checks");
  };

  const sentence = next
    ? <>Next: <b>{LANE_NAME[next.lane]}</b>, #{next.position} of {next.of} in line, {eta(next.eta_hours)}. </>
    : null;

  return (
    <section className={`panel pr-status tone-${stateTone(standing.state)}`} id="pr-status">
      <div className="pr-status-top">
        <span className={`chip chip-${stateTone(standing.state)}`}>{stateLabel(standing.state)}</span>
        <span className="muted small">
          {standing.state === "stalled" ? "Your move · Prospector stopped trying" : OWNER_LINE[standing.owner] ?? ""}
        </span>
      </div>
      <p className="pr-status-sentence">{sentence}{standing.reason}</p>
      {standing.state === "stalled" && tried.length > 0 && (
        <ul className="pr-status-tried small muted">
          {tried.map((t, i) => <li key={i}>{t.at.slice(0, 10)} · {t.lane} {t.stage}: {t.words}</li>)}
        </ul>
      )}
      <div className="pr-status-path small">
        {stages.map((s, i) => (
          <span key={s.key}>
            {i > 0 && <span className="muted"> → </span>}
            <span className={`chip sm ${s.status === "done" ? "chip-green" : s.status === "failed" ? "chip-red"
              : s.status === "current" ? "chip-blue" : "chip-muted"}`} title={s.detail}>
              {s.status === "done" ? "✓ " : s.status === "failed" ? "✗ " : s.status === "current" ? "⏳ " : ""}{s.label}
            </span>
          </span>
        ))}
      </div>
      <div className="pr-status-actions">
        {actionsFor(standing.state, next?.lane ?? null).map((a) => {
          switch (a) {
            case "run-now": return next && (
              <button key={a} className="btn-secondary sm" disabled={busy} onClick={() => onRun(next.lane)}>
                Run {LANE_NAME[next.lane]} now</button>);
            case "merge": return <button key={a} className="btn-primary sm" onClick={() => onScrollTo("pr-actions")}>Merge…</button>;
            case "quick-click": return <button key={a} className="btn-primary sm" disabled={busy} onClick={quickClick}>
              {standing.bucket === "approve-parked" ? "Approve the parked change" : "Take me to it"}</button>;
            case "ask-agent": return seed && (
              <button key={a} className="btn-primary sm" onClick={() => askFresh(seed)}>🤖 Ask the agent why it's stalled</button>);
            case "re-attempt": return <button key={a} className="btn-secondary sm" disabled={busy} onClick={reattempt}>Re-attempt with my notes…</button>;
            case "accept": return <button key={a} className="btn-secondary sm" disabled={busy} onClick={accept}>
              {ours ? "Accept and leave it to the maintainers" : "Accept the blocker and merge…"}</button>;
            case "abandon": return <button key={a} className="btn-secondary sm" onClick={() => onScrollTo("pr-actions")}>Abandon: close the PR…</button>;
          }
        })}
      </div>
    </section>
  );
}
