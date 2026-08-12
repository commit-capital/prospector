import { useCallback, useEffect, useRef, useState } from "react";
import { attachJobStream } from "./jobStream";

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
  track: (key: number, jobId: number) => void;
}

/** Track several independent background jobs through their replayable streams. */
export function useJobGroup(onFinished?: () => void): JobGroupState {
  const [byKey, setByKey] = useState<Record<number, TrackedJob>>({});
  const streams = useRef<Map<number, () => void>>(new Map());
  const notified = useRef(false);
  const onFinishedRef = useRef(onFinished);
  useEffect(() => { onFinishedRef.current = onFinished; }, [onFinished]);

  useEffect(() => () => {
    for (const close of streams.current.values()) close();
    streams.current.clear();
  }, []);

  const track = useCallback((key: number, jobId: number): void => {
    setByKey((current) => ({
      ...current,
      [key]: { id: jobId, status: "queued", finished: false, detail: `job #${jobId} is queued` },
    }));
    const close = attachJobStream(`/api/jobs/${jobId}/stream`, {
      onLog: () => setByKey((current) => {
        const job = current[key];
        if (!job || job.status !== "queued") return current;
        return {
          ...current,
          [key]: { ...job, status: "running", detail: `job #${jobId} is running` },
        };
      }),
      onDone: ({ status, returncode }) => {
        streams.current.delete(jobId);
        setByKey((current) => ({
          ...current,
          [key]: {
            id: jobId,
            status,
            finished: true,
            detail: status === "done"
              ? `job #${jobId} finished`
              : `job #${jobId} failed (exit ${returncode ?? "unknown"})`,
          },
        }));
      },
      onError: () => {
        streams.current.delete(jobId);
        setByKey((current) => ({
          ...current,
          [key]: {
            id: jobId,
            status: "tracking-lost",
            finished: false,
            detail: `lost the live connection to job #${jobId}; the job may still be running`,
          },
        }));
      },
    });
    streams.current.set(jobId, close);
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
