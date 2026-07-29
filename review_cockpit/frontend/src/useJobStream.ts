import { useEffect, useRef, useState } from "react";
import { api } from "./api";

export interface JobStreamState {
  log: string[];
  running: boolean;
}

/** Starts (or reattaches to) one `(kind, target)` job and streams its SSE log.
 *
 *  Jobs run to completion server-side independent of the page that started
 *  them (#683) — closing the tab or navigating away never stops or orphans
 *  one. So on mount this checks for a job matching `kind`+`target` that's
 *  still `running` from an earlier visit and, if found, reattaches to it
 *  instead of showing an empty console just because the browser lost the
 *  original connection; the reattached stream replays the job's full output
 *  so far before following it live. */
export function useJobStream(
  kind: string,
  target: { pr?: number; cluster?: number },
  onDone?: (status: string) => void,
): JobStreamState & { start: (url: string) => void } {
  const [log, setLog] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  // always call the latest onDone without re-subscribing the EventSource for it
  const onDoneRef = useRef(onDone);
  useEffect(() => { onDoneRef.current = onDone; });

  const attach = (url: string) => {
    esRef.current?.close();
    setRunning(true);
    const es = new EventSource(url);
    esRef.current = es;
    es.addEventListener("log", (e: MessageEvent) => setLog((l) => [...l, e.data]));
    es.addEventListener("done", (e: MessageEvent) => {
      const { status } = JSON.parse(e.data) as { status: string };
      setLog((l) => [...l, `■ ${status}`]);
      es.close();
      setRunning(false);
      onDoneRef.current?.(status);
    });
    es.onerror = () => { es.close(); setRunning(false); };
  };

  useEffect(() => {
    let cancelled = false;
    api.jobsList().then((d) => {
      if (cancelled) return;
      const match = d.jobs
        .filter((j) => j.kind === kind && j.status === "running"
          && (target.pr === undefined || j.pr === target.pr)
          && (target.cluster === undefined || j.cluster === target.cluster))
        .sort((a, b) => b.id - a.id)[0];
      if (match) { setLog([]); attach(`/api/jobs/${match.id}/stream`); }
    }).catch(() => {});
    return () => { cancelled = true; esRef.current?.close(); };
  }, [kind, target.pr, target.cluster]);

  const start = (url: string) => {
    if (running) return;
    setLog([]);
    attach(url);
  };

  return { log, running, start };
}
