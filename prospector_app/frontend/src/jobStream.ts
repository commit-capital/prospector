export interface JobCompletion {
  returncode: number | null;
  status: "done" | "failed";
}

interface JobStreamHandlers {
  onLog: (line: string) => void;
  onDone: (completion: JobCompletion) => void;
  onError?: () => void;
}

export interface JobGroupUpdate {
  id: number;
  returncode: number | null;
  status: "queued" | "running" | "done" | "failed";
}

interface JobGroupStreamHandlers {
  onJob: (update: JobGroupUpdate) => void;
  onDone: () => void;
  onError?: () => void;
}

/** Attach to a start-or-reattach job stream and return a close function. */
export function attachJobStream(url: string, handlers: JobStreamHandlers): () => void {
  const es = new EventSource(url);
  const close = (): void => es.close();
  es.addEventListener("log", (e: MessageEvent) => handlers.onLog(e.data));
  es.addEventListener("done", (e: MessageEvent) => {
    const completion = JSON.parse(e.data) as JobCompletion;
    close();
    handlers.onDone(completion);
  });
  es.onerror = (): void => {
    close();
    handlers.onError?.();
  };
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
