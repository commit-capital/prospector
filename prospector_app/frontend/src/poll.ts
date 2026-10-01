import { useEffect } from "react";

/** Run `load` now, then again `ms` after each run settles, so a slow answer
 *  never stacks a second request behind it. A run that falls due while the
 *  page is hidden waits until the page is shown. Returns the function that
 *  stops the poll. */
export function startPoll(load: () => Promise<unknown>, ms: number): () => void {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let stopped = false;
  let due = false;
  const run = async (): Promise<void> => {
    if (document.hidden) {
      due = true;
      return;
    }
    try {
      await load();
    } catch {
      // Each load handles its own failure; a failed run still schedules the next.
    }
    if (!stopped) timer = setTimeout(() => { void run(); }, ms);
  };
  const onVisibility = (): void => {
    if (!due || document.hidden) return;
    due = false;
    void run();
  };
  document.addEventListener("visibilitychange", onVisibility);
  void run();
  return () => {
    stopped = true;
    clearTimeout(timer);
    document.removeEventListener("visibilitychange", onVisibility);
  };
}

/** `startPoll` for a component's lifetime; a new `load` or `ms` restarts it. */
export function usePoll(load: () => Promise<unknown>, ms: number): void {
  useEffect(() => startPoll(load, ms), [load, ms]);
}
