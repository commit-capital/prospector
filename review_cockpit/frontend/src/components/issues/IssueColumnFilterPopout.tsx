import { useEffect, useRef, type ReactNode } from "react";
import type { IssueFilterSpec } from "../../api";
import { EnumFilter, NumFilter } from "../shared/FilterWidgets";

const REPRO_OPTS: { v: "" | "A" | "B" | "C" | "D" | "E" | "F"; label: string }[] = [
  { v: "", label: "Any" },
  { v: "A", label: "A" },
  { v: "B", label: "B" },
  { v: "C", label: "C" },
  { v: "D", label: "D" },
  { v: "E", label: "E" },
  { v: "F", label: "F" },
];

function specSet(
  spec: IssueFilterSpec,
  k: keyof IssueFilterSpec,
  v: unknown,
  onChange: (s: IssueFilterSpec) => void,
) {
  const next = { ...spec };
  if (v === "" || v === undefined || v === null) {
    delete (next as Record<string, unknown>)[k];
  } else {
    (next as Record<string, unknown>)[k] = v;
  }
  onChange(next);
}

function renderContent(
  colKey: string,
  spec: IssueFilterSpec,
  s: (k: keyof IssueFilterSpec, v: unknown) => void,
): ReactNode {
  switch (colKey) {
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

    case "repro":
      return (
        <>
          <div className="cfp-label">Reproduction grade</div>
          <EnumFilter
            opts={REPRO_OPTS}
            current={spec.repro_grade}
            onChange={(v) => s("repro_grade", v)}
          />
        </>
      );

    case "subsystem":
      return (
        <>
          <div className="cfp-label">Subsystem contains</div>
          <input
            type="text"
            className="cfp-text"
            placeholder="e.g. ui, agent…"
            value={spec.subsystem ?? ""}
            onChange={(e) => s("subsystem", e.target.value || undefined)}
            autoFocus
          />
        </>
      );

    case "dups":
      return (
        <>
          <div className="cfp-label">Duplicate count</div>
          <NumFilter
            cmp={spec.dups}
            onCmp={(v) => s("dups", v)}
            placeholder="count"
            min={0}
          />
        </>
      );

    case "prs":
      return (
        <>
          <div className="cfp-label">Linked PR count</div>
          <NumFilter
            cmp={spec.linked_prs}
            onCmp={(v) => s("linked_prs", v)}
            placeholder="count"
            min={0}
          />
        </>
      );

    default:
      return null;
  }
}

export interface IssueColumnFilterProps {
  colKey: string;
  spec: IssueFilterSpec;
  onChange: (next: IssueFilterSpec) => void;
  rect: DOMRect;
  onClose: () => void;
}

export function IssueColumnFilterPopout({ colKey, spec, onChange, rect, onClose }: IssueColumnFilterProps) {
  const ref = useRef<HTMLDivElement>(null);

  const s = (k: keyof IssueFilterSpec, v: unknown) => specSet(spec, k, v, onChange);

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

  const content = renderContent(colKey, spec, s);
  if (!content) return null;

  const POPOUT_W = 230;
  const left = Math.min(rect.left, window.innerWidth - POPOUT_W - 12);

  return (
    <div
      ref={ref}
      className="col-filter-popout"
      style={{ top: rect.bottom + 4, left }}
    >
      {content}
    </div>
  );
}

// Which Issues-table columns have a column-header filter available?
// eslint-disable-next-line react-refresh/only-export-components -- filter metadata co-located with the popout
export const ISSUE_FILTERABLE_COLS = new Set(["author", "pain", "repro", "subsystem", "dups", "prs"]);

// Is a filter currently active for this column?
// eslint-disable-next-line react-refresh/only-export-components -- filter predicate co-located with the popout
export function isIssueColFilterActive(colKey: string, spec: IssueFilterSpec): boolean {
  switch (colKey) {
    case "author":     return spec.author !== undefined;
    case "pain":       return spec.pain !== undefined;
    case "repro":      return spec.repro_grade !== undefined;
    case "subsystem":  return spec.subsystem !== undefined;
    case "dups":       return spec.dups !== undefined;
    case "prs":        return spec.linked_prs !== undefined;
    default:           return false;
  }
}
