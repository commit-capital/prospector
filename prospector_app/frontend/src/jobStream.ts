export interface JobCompletion {
  returncode: number | null;
  status: "done" | "failed";
}

interface JobStreamHandlers {
  onLog: (line: string) => void;
  onDone: (completion: JobCompletion) => void;
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
