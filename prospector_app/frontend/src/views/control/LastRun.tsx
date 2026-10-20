import type { JobRuntime } from "../../api";
import { ago, agoColor, fmt } from "./format";

/** When a phase last ran, as a chip toned by age; a red button instead when
 *  the job feeding it failed since, which opens that job's output. */
export function LastRun({ lastRun, failed, onOpenJob }: {
  lastRun: string | null | undefined;
  failed: JobRuntime | undefined;
  onOpenJob: (id: number) => void;
}) {
  const id = failed?.failed_job;
  if (id != null) {
    return (
      <button type="button" className="chip chip-red chip-button" onClick={() => onOpenJob(id)}
        title={`Job #${id} failed ${fmt(failed?.failed_at)}. Show its output.`}>
        failed {ago(failed?.failed_at)}
      </button>
    );
  }
  return <span className={agoColor(lastRun)} title={fmt(lastRun)}>{ago(lastRun)}</span>;
}
