import { useEffect, useRef, type CSSProperties, type ReactNode } from "react";
import type {
  FilterSpec, Disposition, LocFilter, FilesFilter,
  SafetyFilter, DriftFilter, CiFilter, ResponsesFilter, CheckStatus, CheckClause,
} from "../../api";
import { EnumFilter, NumFilter } from "../shared/FilterWidgets";
import { CHECK_DEFS, CHECK_STATUS_OPTS } from "./checkDefs";

// ---- helpers ----

function pct(v: number): number { return Math.round(v * 100); }
function fromPct(s: string): number { return Number(s) / 100; }

function specSet(
  spec: FilterSpec,
  k: keyof FilterSpec,
  v: unknown,
  onChange: (s: FilterSpec) => void,
) {
  const next = { ...spec };
  if (v === "" || v === undefined || v === null) {
    delete (next as Record<string, unknown>)[k];
  } else {
    (next as Record<string, unknown>)[k] = v;
  }
  onChange(next);
}

// ---- individual filter UIs ----

const SAFETY_OPTS: { v: SafetyFilter | ""; label: string }[] = [
  { v: "", label: "Any" },
  { v: "GREEN", label: "GREEN" },
  { v: "YELLOW", label: "YELLOW" },
  { v: "RED", label: "RED" },
  { v: "not-run", label: "not-run" },
];
const DISP_OPTS: { v: "" | Disposition; label: string }[] = [
  { v: "", label: "Any" },
  { v: "merge", label: "merge" },
  { v: "request-changes", label: "request-changes" },
  { v: "close-dup", label: "close-dup" },
  { v: "close-fixed", label: "close-fixed" },
  { v: "close-stale", label: "close-stale" },
  { v: "needs-human", label: "needs-human" },
];
const DRIFT_OPTS: { v: DriftFilter | ""; label: string }[] = [
  { v: "", label: "Any" },
  { v: "applicable", label: "applicable" },
  { v: "already-fixed", label: "already-fixed" },
  { v: "conflicts", label: "conflicts" },
];
const CI_OPTS: { v: CiFilter | ""; label: string }[] = [
  { v: "", label: "Any" },
  { v: "passing", label: "passing" },
  { v: "failing", label: "failing" },
  { v: "unknown", label: "unknown" },
];
function checkClauseStatuses(spec: FilterSpec, key: string): CheckStatus[] {
  const c = spec.checks?.find((c) => c.key === key);
  if (!c) return [];
  return Array.isArray(c.status) ? c.status : [c.status];
}

// Toggles one status in one check's clause; drops the clause once its last
// status is cleared, and drops `checks` entirely once no clause is left, so
// an inactive per-check filter never lingers as an empty array in the URL.
function toggleCheckStatus(
  spec: FilterSpec, key: string, status: CheckStatus, onChange: (next: FilterSpec) => void,
) {
  const current = checkClauseStatuses(spec, key);
  const nextStatuses = current.includes(status)
    ? current.filter((s) => s !== status)
    : [...current, status];
  const rest = (spec.checks ?? []).filter((c) => c.key !== key);
  const nextChecks = nextStatuses.length
    ? [...rest, { key, status: nextStatuses.length === 1 ? nextStatuses[0] : nextStatuses }]
    : rest;
  const next = { ...spec };
  if (nextChecks.length) next.checks = nextChecks;
  else delete next.checks;
  onChange(next);
}

function allChecksHaveStatus(spec: FilterSpec, defs: readonly { key: string }[], status: CheckStatus): boolean {
  return defs.every((def) => checkClauseStatuses(spec, def.key).includes(status));
}

// Adds (or removes) one status across every check's clause at once, leaving
// each row's other selected statuses untouched — the column-header
// equivalent of clicking every row's button for that status.
function setStatusForAllChecks(
  spec: FilterSpec, defs: readonly { key: string }[], status: CheckStatus, select: boolean,
  onChange: (next: FilterSpec) => void,
) {
  const nextChecks: CheckClause[] = [];
  for (const def of defs) {
    const current = checkClauseStatuses(spec, def.key);
    const nextStatuses = select
      ? (current.includes(status) ? current : [...current, status])
      : current.filter((st) => st !== status);
    if (nextStatuses.length) {
      nextChecks.push({ key: def.key, status: nextStatuses.length === 1 ? nextStatuses[0] : nextStatuses });
    }
  }
  const next = { ...spec };
  if (nextChecks.length) next.checks = nextChecks;
  else delete next.checks;
  onChange(next);
}

const RESPONSES_OPTS: { v: ResponsesFilter | ""; label: string }[] = [
  { v: "", label: "Any" },
  { v: "any", label: "any response" },
  { v: "reopened", label: "↩ reopened" },
  { v: "new_commits", label: "⬆ new commits" },
  { v: "replied", label: "💬 replied" },
  { v: "resubmitted", label: "⤴ resubmitted" },
];

function renderContent(
  colKey: string,
  spec: FilterSpec,
  s: (k: keyof FilterSpec, v: unknown) => void,
  onChange: (next: FilterSpec) => void,
): ReactNode {
  switch (colKey) {
    case "safety":
      return (
        <>
          <div className="cfp-label">Safety verdict</div>
          <EnumFilter
            opts={SAFETY_OPTS}
            current={spec.safety}
            onChange={(v) => s("safety", v)}
          />
        </>
      );

    case "disposition":
      return (
        <>
          <div className="cfp-label">Disposition</div>
          <EnumFilter
            opts={DISP_OPTS}
            current={spec.disposition}
            onChange={(v) => s("disposition", v)}
          />
        </>
      );

    case "drift":
      return (
        <>
          <div className="cfp-label">Drift state</div>
          <EnumFilter
            opts={DRIFT_OPTS}
            current={spec.drift}
            onChange={(v) => s("drift", v)}
          />
        </>
      );

    case "checks":
      return (
        <>
          <div className="cfp-label">CI status</div>
          <EnumFilter
            opts={CI_OPTS}
            current={spec.ci}
            onChange={(v) => s("ci", v)}
          />
          <div className="cfp-row" style={{ marginTop: 8, justifyContent: "space-between" }}>
            <div className="cfp-label" style={{ marginTop: 0 }}>Per-check status</div>
            {(spec.checks?.length ?? 0) > 0 && (
              <button
                className="cfp-clear"
                onClick={() => s("checks", undefined)}
              >Clear all</button>
            )}
          </div>
          <div className="cfp-checks-list">
            <div className="cfp-check-row cfp-check-header-row">
              <span className="cfp-check-row-label">Select all</span>
              <div className="cfp-check-btns">
                {CHECK_STATUS_OPTS.map((opt) => {
                  const allSelected = allChecksHaveStatus(spec, CHECK_DEFS, opt.v);
                  return (
                    <button
                      key={opt.v}
                      type="button"
                      title={allSelected ? `Clear "${opt.label}" from every check` : `Set every check to "${opt.label}"`}
                      className={`cfp-check-btn${allSelected ? " active" : ""}`}
                      onClick={() => setStatusForAllChecks(spec, CHECK_DEFS, opt.v, !allSelected, onChange)}
                    >{opt.short}</button>
                  );
                })}
              </div>
            </div>
            {CHECK_DEFS.map((def) => {
              const selected = checkClauseStatuses(spec, def.key);
              return (
                <div className="cfp-check-row" key={def.key}>
                  <span className="cfp-check-row-label">{def.label}</span>
                  <div className="cfp-check-btns">
                    {CHECK_STATUS_OPTS.map((opt) => (
                      <button
                        key={opt.v}
                        type="button"
                        title={opt.label}
                        className={`cfp-check-btn${selected.includes(opt.v) ? " active" : ""}`}
                        onClick={() => toggleCheckStatus(spec, def.key, opt.v, onChange)}
                      >{opt.short}</button>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </>
      );

    case "updated":
      return (
        <>
          <div className="cfp-label">Author response</div>
          <EnumFilter
            opts={RESPONSES_OPTS}
            current={spec.responses}
            onChange={(v) => s("responses", v)}
          />
        </>
      );

    case "greptile":
      return (
        <>
          <div className="cfp-label">Greptile score (1–5)</div>
          <NumFilter
            cmp={spec.greptile}
            onCmp={(v) => s("greptile", v)}
            placeholder="score"
            min={1}
            step={1}
          />
          <div className="cfp-label" style={{ marginTop: 8 }}>Greptile freshness</div>
          <div className="cfp-row">
            <select
              className="cfp-select"
              value={spec.greptile_stale === true ? "stale" : spec.greptile_stale === false ? "current" : ""}
              onChange={(e) => {
                const v = e.target.value;
                if (!v) s("greptile_stale", undefined);
                else if (v === "stale") s("greptile_stale", true);
                else s("greptile_stale", false);
              }}
            >
              <option value="">Any</option>
              <option value="stale">⚠ Stale (older commit)</option>
              <option value="current">✓ Current (latest commit)</option>
            </select>
          </div>
          <div className="cfp-label" style={{ marginTop: 8 }}>Why the score is what it is</div>
          <div className="cfp-row">
            <select
              className="cfp-select"
              value={spec.greptile_severity ?? ""}
              onChange={(e) => s("greptile_severity", e.target.value || undefined)}
            >
              <option value="">Any</option>
              <option value="defects">🐛 Flagged a real defect</option>
              <option value="nits">🧹 Nitpicks only</option>
            </select>
          </div>
        </>
      );

    case "age":
      return (
        <>
          <div className="cfp-label">Age (days)</div>
          <NumFilter
            cmp={spec.age_days}
            onCmp={(v) => s("age_days", v)}
            placeholder="days"
            min={0}
          />
        </>
      );

    case "author_rate":
      return (
        <>
          <div className="cfp-label">Author merge rate (%)</div>
          <NumFilter
            cmp={spec.author_rate}
            onCmp={(v) => s("author_rate", v)}
            placeholder="e.g. 50"
            min={0}
            step={1}
            convertDisplay={pct}
            convertStore={fromPct}
          />
        </>
      );

    case "pain":
      return (
        <>
          <div className="cfp-label">Community pain score</div>
          <NumFilter
            cmp={spec.pain}
            onCmp={(v) => s("pain", v)}
            placeholder="score"
            min={0}
            step={0.01}
          />
        </>
      );

    case "loc":
      return (
        <>
          <div className="cfp-label">Lines of code</div>
          {spec.loc ? (
            <div className="cfp-loc-knobs">
              <div className="cfp-row">
                <select
                  className="cfp-select"
                  value={spec.loc.metric}
                  onChange={(e) => s("loc", { ...spec.loc!, metric: e.target.value as LocFilter["metric"] })}
                >
                  <option value="both">added + removed</option>
                  <option value="additions">added only</option>
                  <option value="deletions">removed only</option>
                </select>
              </div>
              <div className="cfp-row">
                <select
                  className="cfp-select"
                  value={spec.loc.scope}
                  onChange={(e) => s("loc", { ...spec.loc!, scope: e.target.value as LocFilter["scope"] })}
                >
                  <option value="effective">effective (human lines)</option>
                  <option value="all">all lines</option>
                </select>
              </div>
              <div className="cfp-row">
                <select
                  className="cfp-select"
                  value={spec.loc.op}
                  onChange={(e) => s("loc", { ...spec.loc!, op: e.target.value as LocFilter["op"] })}
                >
                  <option value=">">greater than</option>
                  <option value="<">less than</option>
                </select>
                <input
                  type="number"
                  className="cfp-num"
                  min={0}
                  placeholder="lines"
                  value={spec.loc.value ?? ""}
                  onChange={(e) => s("loc", {
                    ...spec.loc!,
                    value: e.target.value === "" ? undefined : Number(e.target.value),
                  })}
                />
              </div>
              <button
                className="cfp-clear"
                onClick={() => s("loc", undefined)}
              >Clear filter</button>
            </div>
          ) : (
            <button
              className="cfp-opt"
              onClick={() => s("loc", { metric: "both", scope: "effective", op: ">", value: undefined } satisfies LocFilter)}
            >Enable LOC filter</button>
          )}
        </>
      );

    case "files":
      return (
        <>
          <div className="cfp-label">Files changed</div>
          {spec.files ? (
            <div className="cfp-loc-knobs">
              <div className="cfp-row">
                <select
                  className="cfp-select"
                  value={spec.files.op}
                  onChange={(e) => s("files", { ...spec.files!, op: e.target.value as FilesFilter["op"] })}
                >
                  <option value=">">more than</option>
                  <option value="<">fewer than</option>
                </select>
                <input
                  type="number"
                  className="cfp-num"
                  min={0}
                  placeholder="files"
                  value={spec.files.value ?? ""}
                  onChange={(e) => s("files", {
                    ...spec.files!,
                    value: e.target.value === "" ? undefined : Number(e.target.value),
                  })}
                />
              </div>
              <button
                className="cfp-clear"
                onClick={() => s("files", undefined)}
              >Clear filter</button>
            </div>
          ) : (
            <button
              className="cfp-opt"
              onClick={() => s("files", { op: ">", value: undefined } satisfies FilesFilter)}
            >Enable files filter</button>
          )}
        </>
      );

    case "author":
      return (
        <>
          <div className="cfp-label">Author (starts with)</div>
          <input
            type="text"
            className="cfp-text"
            placeholder="username…"
            value={spec.author ?? ""}
            onChange={(e) => s("author", e.target.value || undefined)}
            autoFocus
          />
        </>
      );

    case "cluster":
      return (
        <>
          <div className="cfp-label">Cluster</div>
          <input
            type="number"
            className="cfp-text"
            placeholder="cluster #"
            value={spec.cluster ?? ""}
            onChange={(e) => s("cluster", e.target.value === "" ? undefined : Number(e.target.value.replace(/\D/g, "")))}
          />
          <button
            className={`cfp-opt mt-2 ${spec.cluster_none ? "active" : ""}`}
            onClick={() => {
              const next = { ...spec };
              if (spec.cluster_none) {
                delete next.cluster_none;
              } else {
                delete next.cluster;
                next.cluster_none = true;
              }
              onChange(next);
            }}
          >
            No cluster assigned
          </button>
        </>
      );

    default:
      return null;
  }
}

// ---- the popout itself ----

export interface ColumnFilterProps {
  colKey: string;
  spec: FilterSpec;
  onChange: (next: FilterSpec) => void;
  rect: DOMRect;
  onClose: () => void;
}

export function ColumnFilterPopout({ colKey, spec, onChange, rect, onClose }: ColumnFilterProps) {
  const ref = useRef<HTMLDivElement>(null);

  const s = (k: keyof FilterSpec, v: unknown) => specSet(spec, k, v, onChange);

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const content = renderContent(colKey, spec, s, onChange);
  if (!content) return null;

  // Keep the popout inside the viewport horizontally. The "checks" popout
  // needs extra width for the per-check passed/failed/never-ran button rows.
  const POPOUT_W = colKey === "checks" ? 300 : 230;
  const left = Math.min(rect.left, window.innerWidth - POPOUT_W - 12);

  return (
    <div
      ref={ref}
      className="col-filter-popout"
      style={{ top: rect.bottom + 4, left, "--cfp-w": `${POPOUT_W}px` } as CSSProperties}
    >
      {content}
    </div>
  );
}

// Which columns have a column-header filter available?
// eslint-disable-next-line react-refresh/only-export-components -- filter metadata co-located with the popout
export const FILTERABLE_COLS = new Set([
  "safety", "disposition", "drift", "checks", "updated",
  "score", "greptile", "age", "author_rate", "pain",
  "loc", "files", "author", "cluster",
]);

// Is a filter currently active for this column?
// eslint-disable-next-line react-refresh/only-export-components -- filter predicate co-located with the popout
export function isColFilterActive(colKey: string, spec: FilterSpec): boolean {
  switch (colKey) {
    case "safety":      return spec.safety !== undefined;
    case "disposition": return spec.disposition !== undefined;
    case "drift":       return spec.drift !== undefined;
    case "checks":      return spec.ci !== undefined || (spec.checks?.length ?? 0) > 0;
    case "updated":     return spec.responses !== undefined;
    case "greptile":    return spec.greptile !== undefined || spec.greptile_stale !== undefined
      || spec.greptile_severity !== undefined;
    case "age":         return spec.age_days !== undefined;
    case "author_rate": return spec.author_rate !== undefined;
    case "pain":        return spec.pain !== undefined;
    case "loc":         return spec.loc !== undefined;
    case "files":       return spec.files !== undefined;
    case "author":      return spec.author !== undefined;
    case "cluster":     return spec.cluster !== undefined || spec.cluster_none === true;
    default:            return false;
  }
}
