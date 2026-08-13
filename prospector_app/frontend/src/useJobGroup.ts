import { useCallback, useEffect, useRef, useState } from "react";
import { attachJobGroupStream } from "./jobStream";

export type TrackedJobStatus = "queued" | "running" | "done" | "failed" | "tracking-lost";

export interface TrackedJob {
  finished: boolean;
  id: number;
  status: TrackedJobStatus;
  detail: string;
}

export interface JobGroupState {
  allFinished: boolean;
  byKey: Record<number, TrackedJob>;
  finishedCount: number;
  jobCount: number;
  track: (jobs: Array<{ key: number; jobId: number }>) => void;
}

/** Track several independent background jobs through their replayable streams. */
export function useJobGroup(onFinished?: () => void): JobGroupState {
  const [byKey, setByKey] = useState<Record<number, TrackedJob>>({});
  const closeRef = useRef<(() => void) | null>(null);
  const notified = useRef(false);
  const onFinishedRef = useRef(onFinished);
  useEffect(() => { onFinishedRef.current = onFinished; }, [onFinished]);

  useEffect(() => () => closeRef.current?.(), []);

  const track = useCallback((jobs: Array<{ key: number; jobId: number }>): void => {
    if (jobs.length === 0) return;
    closeRef.current?.();
    notified.current = false;
    const keyById = new Map(jobs.map(({ key, jobId }) => [jobId, key]));
    const initial: Record<number, TrackedJob> = {};
    for (const { key, jobId } of jobs) {
      initial[key] = {
        id: jobId, status: "queued", finished: false, detail: `job #${jobId} is queued`,
      };
    }
    setByKey(initial);
    closeRef.current = attachJobGroupStream(jobs.map(({ jobId }) => jobId), {
      onJob: ({ id, status, returncode }) => setByKey((current) => {
        const key = keyById.get(id);
        if (key === undefined) return current;
        return {
          ...current,
          [key]: {
            id,
            status,
            finished: status === "done" || status === "failed",
            detail: status === "queued"
              ? `job #${id} is queued`
              : status === "running"
                ? `job #${id} is running`
                : status === "done"
                  ? `job #${id} finished`
                  : `job #${id} failed (exit ${returncode ?? "unknown"})`,
          },
        };
      }),
      onDone: () => { closeRef.current = null; },
      onError: () => {
        closeRef.current = null;
        setByKey((current) => {
          const next: Record<number, TrackedJob> = {};
          for (const [key, job] of Object.entries(current)) {
            next[Number(key)] = job.finished ? job : {
              ...job,
              status: "tracking-lost",
              detail: `lost the live connection to job #${job.id}; the job may still be running`,
            };
          }
          return next;
        });
      },
    });
  }, []);

  const jobs = Object.values(byKey);
  const finishedCount = jobs.filter((job) => job.finished).length;
  const allFinished = jobs.length > 0 && finishedCount === jobs.length;
  useEffect(() => {
    if (!allFinished || notified.current) return;
    notified.current = true;
    onFinishedRef.current?.();
  }, [allFinished]);

  return { allFinished, byKey, finishedCount, jobCount: jobs.length, track };
}
