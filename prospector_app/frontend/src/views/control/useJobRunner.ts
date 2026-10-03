import { useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { useExec } from "../../ExecContext";
import { attachJobStream, type JobMeta } from "../../jobStream";
import { fmtElapsed } from "./format";

export interface JobRunner {
  /** The attached job's output so far. */
  log: string[];
  /** The kind of the job running now, if any. */
  running: string | null;
  meta: JobMeta | null;
  lastOutputAt: number | null;
  reconnecting: boolean;
  /** Start a job (a `/api/jobs/run/...` URL) or attach to one (`/api/jobs/<id>/stream`). */
  attach: (url: string, kind: string, firstLine: string) => void;
  stop: (id: number) => void;
  /** Close the job line once its job has ended. */
  dismiss: () => void;
}

/** The one job this page follows. A job runs server-side whatever this page
 *  does (#683): attaching replays its output so far and follows it live, and
 *  closing the stream never touches the job. On load it reattaches to a job
 *  still queued or running. A job that ends while followed raises a toast and
 *  calls `onSettled`. */
export function useJobRunner(onSettled: () => void): JobRunner {
  const { pushToast } = useExec();
  const [log, setLog] = useState<string[]>([]);
  const [running, setRunning] = useState<string | null>(null);
  const [meta, setMeta] = useState<JobMeta | null>(null);
  const [lastOutputAt, setLastOutputAt] = useState<number | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const closeRef = useRef<(() => void) | null>(null);
  const settledRef = useRef(onSettled);
  useEffect(() => { settledRef.current = onSettled; });

  const attach = (url: string, kind: string, firstLine: string) => {
    closeRef.current?.();
    setLog([firstLine]);
    setRunning(kind);
    setMeta(null);
    setLastOutputAt(null);
    setReconnecting(false);
    let watched: JobMeta | null = null;
    const settle = () => {
      setRunning(null); setReconnecting(false); settledRef.current();
    };
    closeRef.current = attachJobStream(url, {
      onJob: (m) => {
        setMeta(m);
        setReconnecting(false);
        if (m.last_output) setLastOutputAt(new Date(m.last_output).getTime());
        if (m.status === "queued" || m.status === "running") watched = m;
        else setRunning(null);
      },
      onLog: (line, live) => {
        setLog((l) => [...l, line]);
        if (live) {
          setLastOutputAt(Date.now());
          setMeta((m) => (m && m.status === "queued" ? { ...m, status: "running" } : m));
        }
      },
      onReconnecting: () => setReconnecting(true),
      onDone: (c) => {
        setLog((l) => [...l, `■ job ${c.status}`]);
        setMeta((m) => m && { ...m, status: c.status, finished: c.finished, returncode: c.returncode });
        settle();
        if (watched) {
          const took = c.finished
            ? ` in ${fmtElapsed((new Date(c.finished).getTime() - new Date(watched.started).getTime()) / 1000)}`
            : "";
          if (c.status === "done") pushToast(`${watched.label} finished${took}`, "green");
          else pushToast(`${watched.label} failed${c.returncode != null ? ` (exit ${c.returncode})` : ""}${took}`, "red",
            { detail: "Its output is at the top of the Health & queues tab." });
        }
      },
      onError: (named) => {
        setLog((l) => [...l, named
          ? "⚠ lost the job's output stream — reload this page to reattach"
          : "⚠ the job didn't start: the server refused it (one may already be running, or the count is invalid) or couldn't be reached"]);
        settle();
      },
    });
  };

  const stop = (id: number) => {
    api.stopJob(id).catch((e) => setLog((l) => [...l, `⚠ couldn't stop job #${id}: ${String(e)}`]));
  };

  const dismiss = () => {
    if (running) return;
    closeRef.current?.();
    closeRef.current = null;
    setLog([]);
    setMeta(null);
  };

  useEffect(() => {
    api.jobsList().then((d) => {
      const live = d.jobs.filter((j) => j.status === "queued" || j.status === "running")
        .sort((a, b) => b.id - a.id)[0];
      if (live) attach(`/api/jobs/${live.id}/stream`, live.kind,
        `↻ reattached to job #${live.id} (${live.label}) — replaying its output so far…`);
    }).catch(() => {});
    return () => closeRef.current?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only: reattach check runs once
  }, []);

  return { log, running, meta, lastOutputAt, reconnecting, attach, stop, dismiss };
}
