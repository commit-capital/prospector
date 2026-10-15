import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { usePoll } from "../../poll";
import { nextLines } from "./logBuffer";

const KEEP_LINES = 500;

/** This machine's worker log, followed: its tail on open, then each new line
 *  as the worker writes it. Stays at the bottom unless the reader scrolled up. */
export function LogTail() {
  const [lines, setLines] = useState<string[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const offset = useRef<number | null>(null);
  const file = useRef<string | null>(null);
  const box = useRef<HTMLPreElement>(null);
  const pinned = useRef(true);

  const load = useCallback(() => api.workerLog(offset.current, file.current).then((r) => {
    offset.current = r.offset;
    file.current = r.file;
    setMissing(!r.exists);
    setErr(null);
    if (r.lines.length > 0 || r.skipped) setLines((prev) => nextLines(prev, r, KEEP_LINES));
  }, (e: unknown) => setErr(e instanceof Error ? e.message : String(e))), []);
  usePoll(load, 2_000);

  useEffect(() => {
    const el = box.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const onScroll = () => {
    const el = box.current;
    if (el) pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  };

  return (
    <div className="log-tail">
      {err && <div className="activity-bad small">Couldn't read the log: {err}</div>}
      {missing ? <div className="muted small">This machine's workers have not written a log yet.</div>
        : <pre ref={box} onScroll={onScroll} className="log-tail-body">{lines.join("\n")}</pre>}
    </div>
  );
}
