import { Fragment, useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router";
import type { ActivityView, FilterSpec, MachineActivity } from "../../api";
import { PRLink } from "../../components/PRLink";
import { FlyoutIssueLink } from "../../components/FlyoutIssueLink";
import { Panel } from "./Panel";
import { activityPhrases, machineStatus, type ActivityPhrase, type PhrasePart } from "./activitySentence";

function prsLink(numbers: number[]): string {
  const spec: FilterSpec = { numbers, state: "all" };
  return `/prs/list?spec=${encodeURIComponent(JSON.stringify(spec))}`;
}

/** One piece of the sentence: a link to its PRs, a button that opens a job's
 *  output, or plain text. */
function Part({ part, linkKind, onOpenJob }: {
  part: PhrasePart;
  linkKind: ActivityPhrase["linkKind"];
  onOpenJob: (jobId: number) => void;
}) {
  const cls = part.bad ? "activity-bad" : undefined;
  if (part.jobId != null) {
    const id = part.jobId;
    return (
      <button className={`linkish ${cls ?? ""}`} style={{ fontSize: "inherit", padding: 0 }}
        title="Show this job's output" onClick={() => onOpenJob(id)}>{part.text}</button>
    );
  }
  if (linkKind === "pr" && part.numbers && part.numbers.length > 0) {
    return (
      <Link to={prsLink(part.numbers)} className={cls}
        title={`Open ${part.numbers.length} PR${part.numbers.length === 1 ? "" : "s"} in PR Explorer`}>
        {part.text}
      </Link>
    );
  }
  return <span className={cls}>{part.text}</span>;
}

function Phrase({ p, onOpenJob }: { p: ActivityPhrase; onOpenJob: (jobId: number) => void }) {
  const parts = p.parts.map((part, i) => (
    <Fragment key={i}>{i > 0 && ", "}<Part part={part} linkKind={p.linkKind} onOpenJob={onOpenJob} /></Fragment>
  ));
  if (p.style === "plain") return <span className="muted">{p.lead.text}</span>;
  const lead = <Part part={p.lead} linkKind={p.linkKind} onOpenJob={onOpenJob} />;
  if (p.style === "list") return <>{lead} {parts}</>;
  return <>{lead}{parts.length > 0 && <> ({parts})</>}</>;
}

function Machine({ m, now, onOpenJob }: { m: MachineActivity; now: number; onOpenJob: (jobId: number) => void }) {
  const phrases = activityPhrases(m);
  const sentence: ReactNode[] = phrases.map((p, i) => (
    <Fragment key={p.key}>{i > 0 && " · "}<Phrase p={p} onOpenJob={onOpenJob} /></Fragment>
  ));
  return (
    <div className="cpanel-row">
      <div className="cpanel-row-head">
        <b className={m.host === "unattributed" ? undefined : "mono"}>{m.host === "unattributed" ? "Unattributed" : m.host}</b>
        {m.local && <span className="chip chip-muted sm">this machine</span>}
        {machineStatus(m, now).map((s) => (
          <span key={s.text} className={`status-${s.tone}`}
            title={m.host === "unattributed" ? "Runs written before every machine recorded its name on them." : undefined}>
            {s.text}
          </span>
        ))}
        {m.current.pr != null && <span className="muted small">· now on <PRLink n={m.current.pr} /></span>}
        {m.current.issue != null && <span className="muted small">· now on <FlyoutIssueLink n={m.current.issue} /></span>}
      </div>
      <div className="activity-line">{sentence.length > 0 ? sentence : <span className="muted">idle</span>}</div>
    </div>
  );
}

/** What each machine did in the past day, one sentence per machine. */
export function RecentActivity({ activity, onOpenJob }: {
  activity: ActivityView | null;
  onOpenJob: (jobId: number) => void;
}) {
  // Offline durations read against a clock that ticks each minute.
  const [now, setNow] = useState<number>(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(t);
  }, []);
  return (
    <Panel id="recent-activity" title="Recent activity"
      meta={`past ${activity?.window_hours ?? 24} hours, per machine`}>
      {activity == null
        ? <div className="muted small">Loading…</div>
        : activity.machines.length === 0
          ? <div className="muted small">No machine has recorded any work in the past day.</div>
          : activity.machines.map((m) => <Machine key={m.host} m={m} now={now} onOpenJob={onOpenJob} />)}
    </Panel>
  );
}
