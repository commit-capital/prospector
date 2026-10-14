import { useEffect, useRef, useState } from "react";
import type { JobMeta, QueueSpot } from "../../jobStream";
import { fmtElapsed } from "./format";
import type { JobRunner } from "./useJobRunner";

/** Where a queued job stands: how many of its kind wait ahead of it and the
 *  jobs running now, each a button that switches the console to it. */
function QueueNote({ queue, onOpen }: { queue: QueueSpot | null; onOpen: (id: number) => void }) {
  if (queue == null) return <> — waiting for another run of this job to finish</>;
  const running = queue.running.map((r, i) => {
    const target = r.pr != null ? ` (PR #${r.pr})` : r.cluster != null ? ` (cluster ${r.cluster})` : "";
    return (
      <span key={r.id}>{i > 0 && ", "}
        <button className="linkish" style={{ fontSize: "inherit", padding: 0 }}
          title="Show this job's output" onClick={() => onOpen(r.id)}>job #{r.id}</button>{target}
      </span>
    );
  });
  return (
    <> — {queue.ahead === 0 ? "next in line" : `${queue.ahead} ahead`}
      {running.length > 0 && <>; now running {running}</>}</>
  );
}

/** The line above a job's console: queued or running and for how long, how
 *  long since it last printed, a Stop button, or how it ended. Ticks each
 *  second while the job runs. */
function JobStatus({ meta, running, reconnecting, lastOutputAt, onStop, onOpen }: {
  meta: JobMeta;
  running: boolean;
  reconnecting: boolean;
  lastOutputAt: number | null;
  onStop: () => void;
  onOpen: (id: number) => void;
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
          ? <span><b>{meta.label}</b> (job #{meta.id}) queued<QueueNote queue={meta.queue} onOpen={onOpen} /></span>
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
              lastOutputAt={runner.lastOutputAt} onStop={() => runner.meta && runner.stop(runner.meta.id)}
              onOpen={(id) => runner.meta && runner.attach(`/api/jobs/${id}/stream`, runner.meta.kind,
                `↻ attached to job #${id} (${runner.meta.label}) — replaying its output so far…`)} />
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
