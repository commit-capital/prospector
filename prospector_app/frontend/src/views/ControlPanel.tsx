import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { api, type Autohunt, type CapacityState, type ControlOverview, type FixQueue, type JobRuntime, type JobSpec, type PipelineStatus, type SuggestedAction } from "../api";
import { usePoll } from "../poll";
import { Attention } from "./control/Attention";
import { Capacity } from "./control/Capacity";
import { Coverage } from "./control/Coverage";
import { JobLine } from "./control/JobLine";
import { Jobs } from "./control/Jobs";
import { Lanes } from "./control/Lanes";
import { RunHistory } from "./control/RunHistory";
import { StatusLine } from "./control/StatusLine";
import { Timeline } from "./control/Timeline";
import { RANGE_OPTIONS, type RangeOpt } from "./control/format";
import { useJobRunner } from "./control/useJobRunner";

type Tab = "jobs" | "capacity" | "history";
const TAB_KEY = "control.tab";

function readTab(): Tab {
  try {
    const v = localStorage.getItem(TAB_KEY);
    return v === "capacity" || v === "history" ? v : "jobs";
  } catch {
    return "jobs";
  }
}

/** Pipeline → Health & queues: every machine in a line, the problems that
 *  need a person, each lane's now and next, a timeline, and the secondary
 *  panels in tabs. */
export default function ControlPanel() {
  const [overview, setOverview] = useState<ControlOverview | null>(null);
  const [overviewErr, setOverviewErr] = useState<string>();
  const [open, setOpen] = useState<string | null>(null);
  // ?job=<kind> opens that job's row, as a page's link to it does.
  const focusJob = useSearchParams()[0].get("job");
  const [tab, setTab] = useState<Tab>(() => (focusJob ? "jobs" : readTab()));
  const [specs, setSpecs] = useState<JobSpec[]>([]);
  const [runtimes, setRuntimes] = useState<Record<string, JobRuntime> | null>(null);
  const [pipeline, setPipeline] = useState<PipelineStatus | null>(null);
  const [suggestions, setSuggestions] = useState<SuggestedAction[] | null>(null);
  const [capacity, setCapacity] = useState<CapacityState | null>(null);
  const [hunt, setHunt] = useState<Autohunt | null>(null);
  const [fixQueue, setFixQueue] = useState<FixQueue | null>(null);
  const [range, setRange] = useState<RangeOpt>(RANGE_OPTIONS[0]);

  const fetchOverview = useCallback((fresh: boolean) => api.controlOverview(fresh)
    .then((o) => { setOverview(o); setOverviewErr(undefined); })
    .catch((e) => setOverviewErr(String(e))), []);
  const loadOverview = useCallback(() => fetchOverview(false), [fetchOverview]);
  // After an action of its own the page reads past the shared cache.
  const refreshOverview = useCallback(() => { void fetchOverview(true); }, [fetchOverview]);
  const loadPipeline = useCallback(() => {
    api.pipelineStatus().then(setPipeline).catch(() => {});
    api.suggestedActions("all").then((d) => setSuggestions(d.items)).catch(() => setSuggestions([]));
    api.jobRuntimes().then((d) => setRuntimes(d.runtimes)).catch(() => {});
  }, []);
  const loadFixQueue = useCallback(
    () => api.fixQueue(range.days, range.allTime).then(setFixQueue).catch(() => {}), [range]);
  const loadHunt = useCallback(
    () => api.autohunt(range.days, range.allTime).then(setHunt).catch(() => {}), [range]);
  const loadCapacity = useCallback(() => api.capacity().then(setCapacity).catch(() => {}), []);
  const noop = useCallback(() => Promise.resolve(), []);

  const runner = useJobRunner(() => { loadPipeline(); refreshOverview(); });
  useEffect(() => {
    api.jobSpecs().then((d) => setSpecs(d.specs)).catch(() => {});
    loadPipeline();
  }, [loadPipeline]);

  // Scroll to the linked job once the panels above it have loaded.
  const scrolled = useRef(false);
  const loaded = overview != null && runtimes != null && suggestions != null && specs.length > 0;
  useEffect(() => {
    if (!focusJob || !loaded || scrolled.current) return;
    scrolled.current = true;
    document.getElementById(`job-${focusJob}`)?.scrollIntoView({ block: "center" });
  }, [focusJob, loaded]);

  const busy = (overview?.lanes ?? []).some((l) => l.now.length > 0);
  usePoll(loadOverview, busy ? 10_000 : 30_000);
  usePoll(open === "autofix" || tab === "history" ? loadFixQueue : noop, 15_000);
  usePoll(tab === "history" ? loadHunt : noop, 30_000);
  usePoll(tab === "capacity" ? loadCapacity : noop, 30_000);

  const openJob = (id: number) => {
    runner.attach(`/api/jobs/${id}/stream`, "", `↻ job #${id}'s output…`);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const pickTab = (t: Tab) => {
    setTab(t);
    try { localStorage.setItem(TAB_KEY, t); } catch { /* the tab resets next visit */ }
  };

  return (
    <div className="control">
      <div className="detail-head"><h1>🎛️ Health &amp; queues</h1></div>
      <JobLine runner={runner} />
      {overviewErr && !overview && <div className="error">Couldn't load the overview: {overviewErr}</div>}
      {overview == null ? <div className="muted small">Loading…</div> : (
        <>
          <StatusLine overview={overview} onChanged={refreshOverview} />
          <Attention overview={overview} onChanged={refreshOverview}
            onReviewParked={() => { setOpen("autofix"); loadFixQueue(); }} />
          <Lanes overview={overview} open={open}
            onToggle={(l) => setOpen((o) => (o === l ? null : l))}
            fixQueue={fixQueue} onFixChanged={loadFixQueue} onChanged={refreshOverview} />
          <Timeline overview={overview} />
        </>
      )}
      <div className="ctabs" role="tablist">
        {([["jobs", "Coverage and jobs"], ["capacity", "AI capacity"], ["history", "Run history"]] as const).map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => pickTab(k)}>{label}</button>
        ))}
      </div>
      {tab === "jobs" && (
        <>
          <Coverage pipeline={pipeline} runtimes={runtimes} onOpenJob={openJob} />
          <Jobs specs={specs} runtimes={runtimes} suggestions={suggestions ?? []} pipeline={pipeline}
            focus={focusJob} running={runner.running} onOpenJob={openJob}
            onStart={(url, spec) => {
              runner.attach(url, spec.kind, `▶ starting ${spec.label}…`);
              window.scrollTo({ top: 0, behavior: "smooth" });
            }} />
        </>
      )}
      {tab === "capacity" && <Capacity state={capacity} />}
      {tab === "history" && <RunHistory hunt={hunt} fixQueue={fixQueue} range={range} onRange={setRange} />}
    </div>
  );
}
