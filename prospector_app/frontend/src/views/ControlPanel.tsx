import { useCallback, useEffect, useState } from "react";
import { api, type ActivityView, type Autohunt, type CapacityState, type FixQueue, type JobRuntime, type JobSpec, type PipelineStatus, type SuggestedAction, type VerifyQueue } from "../api";
import { usePoll } from "../poll";
import { Capacity } from "./control/Capacity";
import { Coverage } from "./control/Coverage";
import { JobLine } from "./control/JobLine";
import { Jobs } from "./control/Jobs";
import { NeedsYou } from "./control/NeedsYou";
import { Queues } from "./control/Queues";
import { RecentActivity } from "./control/RecentActivity";
import { RunHistory } from "./control/RunHistory";
import { RANGE_OPTIONS, type RangeOpt } from "./control/format";
import { useJobRunner } from "./control/useJobRunner";

/** Pipeline → Health & queues: what needs you, what every machine did in the
 *  past day, the worker queues, phase coverage, the jobs to run, and — folded
 *  — AI capacity and the run history. */
export default function ControlPanel() {
  const [specs, setSpecs] = useState<JobSpec[]>([]);
  const [specsErr, setSpecsErr] = useState<string>();
  const [runtimes, setRuntimes] = useState<Record<string, JobRuntime> | null>(null);
  const [pipeline, setPipeline] = useState<PipelineStatus | null>(null);
  const [suggestions, setSuggestions] = useState<SuggestedAction[]>([]);
  const [activity, setActivity] = useState<ActivityView | null>(null);
  const [capacity, setCapacity] = useState<CapacityState | null>(null);
  const [hunt, setHunt] = useState<Autohunt | null>(null);
  const [verifyQueue, setVerifyQueue] = useState<VerifyQueue | null>(null);
  const [fixQueue, setFixQueue] = useState<FixQueue | null>(null);
  const [range, setRange] = useState<RangeOpt>(RANGE_OPTIONS[0]);

  const loadPipeline = useCallback(() => {
    api.pipelineStatus().then(setPipeline).catch(() => {});
    api.suggestedActions("all").then((d) => setSuggestions(d.items)).catch(() => {});
    api.jobRuntimes().then((d) => setRuntimes(d.runtimes)).catch(() => {});
  }, []);
  const loadActivity = useCallback(() => api.machinesActivity().then(setActivity).catch(() => {}), []);
  const loadCapacity = useCallback(() => api.capacity().then(setCapacity).catch(() => {}), []);
  const loadHunt = useCallback(
    () => api.autohunt(range.days, range.allTime).then(setHunt).catch(() => {}), [range]);
  const loadVerifyQueue = useCallback(() => api.verifyQueue(7, false).then(setVerifyQueue).catch(() => {}), []);
  const loadFixQueue = useCallback(
    () => api.fixQueue(range.days, range.allTime).then(setFixQueue).catch(() => {}), [range]);

  const runner = useJobRunner(() => { loadPipeline(); loadActivity(); });

  useEffect(() => {
    api.jobSpecs().then((d) => setSpecs(d.specs)).catch((e) => setSpecsErr(String(e)));
    loadPipeline();
  }, [loadPipeline]);

  const fixRunning = (fixQueue?.queue ?? []).some((e) => e.status === "running" || e.status === "pushing");
  usePoll(loadActivity, 30_000);
  usePoll(loadCapacity, 30_000);
  usePoll(loadHunt, 30_000);
  usePoll(loadVerifyQueue, 30_000);
  // A mechanical action runs for a minute or two, so an in-flight queue is
  // polled fast enough to show it moving through its steps.
  usePoll(loadFixQueue, fixRunning ? 10_000 : 30_000);

  const openJob = (jobId: number) => {
    const job = activity?.machines.flatMap((m) => m.jobs).find((j) => j.job_id === jobId);
    if (!job) return;
    runner.attach(`/api/jobs/${jobId}/stream`, job.kind, `↻ job #${jobId} (${job.label}) — replaying its output…`);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  return (
    <div className="control">
      <div className="detail-head">
        <h1>🎛️ Health &amp; queues</h1>
      </div>
      {specsErr && !specs.length && <div className="error">Couldn't load pipeline jobs: {specsErr}</div>}
      <JobLine runner={runner} />
      <NeedsYou hunt={hunt} activity={activity} capacity={capacity} onResume={loadHunt} />
      <RecentActivity activity={activity} onOpenJob={openJob} />
      <Queues hunt={hunt} verifyQueue={verifyQueue} fixQueue={fixQueue} onFixChanged={loadFixQueue} />
      <Coverage pipeline={pipeline} />
      <Jobs specs={specs} runtimes={runtimes} suggestions={suggestions} pipeline={pipeline}
        running={runner.running}
        onStart={(url, spec) => {
          runner.attach(url, spec.kind, `▶ starting ${spec.label}…`);
          window.scrollTo({ top: 0, behavior: "smooth" });
        }} />
      <Capacity state={capacity} />
      <RunHistory hunt={hunt} fixQueue={fixQueue} range={range} onRange={setRange} />
    </div>
  );
}
