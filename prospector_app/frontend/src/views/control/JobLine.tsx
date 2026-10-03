import { useEffect, useRef, useState } from "react";
import type { JobMeta } from "../../jobStream";
import { fmtElapsed } from "./format";
import type { JobRunner } from "./useJobRunner";

/** The line above a job's console: queued or running and for how long, how
 *  long since it last printed, a Stop button, or how it ended. Ticks each
 *  second while the job runs. */
function JobStatus({ meta, running, reconnecting, lastOutputAt, onStop }: {
  meta: JobMeta;
  running: boolean;
  reconnecting: boolean;
  lastOutputAt: number | null;
  onStop: () => void;
}) {
  const [now, setNow] = useState<number>(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(t);
  }, [running]);
  const started = new Date(meta.started).getTime();
  if (reconnecting) {
    return (
      <span className="jobstatus-warn">
        ↻ Lost the connection to the server (it may be restarting). Reconnecting — {meta.label} keeps running.
      </span>
    );
  }
  if (running) {
    const quiet = lastOutputAt != null ? (now - lastOutputAt) / 1000 : null;
    return (
      <>
        <span className="jobstatus-dot" />
        {meta.status === "queued"
          ? <span><b>{meta.label}</b> (job #{meta.id}) queued — waiting for another run of this job to finish</span>
          : <span><b>{meta.label}</b> (job #{meta.id}) running · {fmtElapsed((now - started) / 1000)}
            {quiet != null && quiet >= 10 && <> · no output for {fmtElapsed(quiet)}</>}</span>}
        <button className="btn-secondary sm" onClick={onStop}
          title="Stop this job: every process it started, its agents included, gets SIGTERM, then SIGKILL 10s later; a verify job's sandbox containers are removed.">■ Stop</button>
      </>
    );
  }
  const took = meta.finished ? fmtElapsed((new Date(meta.finished).getTime() - started) / 1000) : null;
  if (meta.status === "failed") {
    return (
      <span className="jobstatus-bad">
        ✗ <b>{meta.label}</b> (job #{meta.id}) failed{meta.returncode != null && ` (exit ${meta.returncode})`}{took && ` after ${took}`}
      </span>
    );
  }
  return <span className="jobstatus-ok">✓ <b>{meta.label}</b> (job #{meta.id}) finished{took && ` in ${took}`}</span>;
}

/** The job this page follows, pinned above every panel: its status line, its
 *  output, and a close button once it has ended. Nothing while no job is
 *  attached. */
export function JobLine({ runner }: { runner: JobRunner }) {
  const [showOutput, setShowOutput] = useState(true);
  const logRef = useRef<HTMLDivElement>(null);
  useEffect(() => { logRef.current?.scrollTo(0, logRef.current.scrollHeight); }, [runner.log, showOutput]);
  if (!runner.meta && runner.log.length === 0) return null;
  const live = runner.running != null
    && (runner.meta == null || runner.meta.status === "queued" || runner.meta.status === "running");
  return (
    <div className="jobline">
      <div className="jobstatus">
        {runner.meta
          ? <JobStatus meta={runner.meta} running={live} reconnecting={runner.reconnecting}
              lastOutputAt={runner.lastOutputAt} onStop={() => runner.meta && runner.stop(runner.meta.id)} />
          : <span>{runner.log[0]}</span>}
        <button className="linkish small" onClick={() => setShowOutput((v) => !v)} aria-expanded={showOutput}
          style={live ? undefined : { marginLeft: "auto" }}>
          {showOutput ? "hide output ▴" : "output ▾"}
        </button>
        {!live && <button className="btn-secondary sm" style={{ marginLeft: 6 }} aria-label="Close the job output" onClick={runner.dismiss}>✕</button>}
      </div>
      {showOutput && (
        <div className="joblog" ref={logRef}>
          {runner.log.map((l, i) => <div key={i} className="logline">{l}</div>)}
        </div>
      )}
    </div>
  );
}
