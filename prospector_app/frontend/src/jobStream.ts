import type { JobStatus } from "./api";

export interface JobCompletion {
  returncode: number | null;
  status: "done" | "failed";
  finished: string | null;
}

/** The `job` event a job stream opens with: the job's record, plus when its
 *  output last changed. */
export interface JobMeta {
  id: number;
  kind: string;
  label: string;
  status: JobStatus;
  started: string;
  finished: string | null;
  returncode: number | null;
  last_output: string | null;
  /** Lines the job had already logged when this stream opened. */
  lines: number;
}

interface JobStreamHandlers {
  /** `live` is false for a line the job logged before this stream opened. */
  onLog: (line: string, live: boolean) => void;
  onDone: (completion: JobCompletion) => void;
  onJob?: (meta: JobMeta) => void;
  /** The connection dropped mid-job; the stream is retrying. */
  onReconnecting?: () => void;
  /** The stream ended without a `done` event. `named` is false when it never
   *  named a job: the server refused to start one, or could not be reached. */
  onError?: (named: boolean) => void;
}

export interface JobGroupUpdate {
  id: number;
  returncode: number | null;
  status: JobStatus;
}

interface JobGroupStreamHandlers {
  onJob: (update: JobGroupUpdate) => void;
  onDone: () => void;
  onError?: () => void;
}

// A dropped job stream retries this often, this many times — long enough to
// ride out the backend restarting under a dev-server reload.
const RECONNECT_MS = 2000;
const RECONNECT_ATTEMPTS = 45;

/** Attach to a start-or-reattach job stream and return a close function. Once
 *  the stream has named its job, a dropped connection reconnects to that job
 *  and resumes after the last line received, so the backend restarting
 *  mid-job neither ends nor repeats the output. */
export function attachJobStream(url: string, handlers: JobStreamHandlers): () => void {
  let es: EventSource | null = null;
  let jobId: number | null = null;
  let received = 0;
  let replayed = 0;
  let attempts = 0;
  let closed = false;
  let retry: number | undefined;
  const close = (): void => {
    closed = true;
    window.clearTimeout(retry);
    es?.close();
  };
  const connect = (target: string): void => {
    const source = new EventSource(target);
    es = source;
    source.addEventListener("job", (e: MessageEvent) => {
      const meta = JSON.parse(e.data) as JobMeta;
      jobId = meta.id;
      replayed = meta.lines;
      attempts = 0;
      handlers.onJob?.(meta);
    });
    source.addEventListener("log", (e: MessageEvent) => {
      received += 1;
      handlers.onLog(e.data, received > replayed);
    });
    source.addEventListener("done", (e: MessageEvent) => {
      close();
      handlers.onDone(JSON.parse(e.data) as JobCompletion);
    });
    source.onerror = (): void => {
      source.close();
      if (closed) return;
      if (jobId === null || attempts >= RECONNECT_ATTEMPTS) {
        closed = true;
        handlers.onError?.(jobId !== null);
        return;
      }
      attempts += 1;
      if (attempts === 1) handlers.onReconnecting?.();
      retry = window.setTimeout(
        () => connect(`/api/jobs/${jobId}/stream?after=${received}`), RECONNECT_MS);
    };
  };
  connect(url);
  return close;
}

/** Follow several jobs through one replayable SSE connection. */
export function attachJobGroupStream(
  jobIds: number[],
  handlers: JobGroupStreamHandlers,
): () => void {
  const params = new URLSearchParams();
  for (const id of jobIds) params.append("job_id", String(id));
  const es = new EventSource(`/api/jobs/stream/group?${params}`);
  const close = (): void => es.close();
  es.addEventListener("job", (e: MessageEvent) => {
    handlers.onJob(JSON.parse(e.data) as JobGroupUpdate);
  });
  es.addEventListener("done", () => {
    close();
    handlers.onDone();
  });
  es.onerror = (): void => {
    close();
    handlers.onError?.();
  };
  return close;
}
