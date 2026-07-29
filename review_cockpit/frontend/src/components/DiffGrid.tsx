import { Fragment, useEffect, useRef, useState } from "react";
import { parseDiff, Diff, Hunk, type HunkData } from "react-diff-view";
import "react-diff-view/style/index.css";
import { api, type PRRow } from "../api";

type ParsedFile = ReturnType<typeof parseDiff>[number];
interface PRDiff { pr: number; loading: boolean; error: string | null; files: Map<string, ParsedFile>; }

const MIN_COL = 300; // px per column before the grid scrolls horizontally

// Group columns by what we're doing with each PR (#185): merge candidates on the
// left, a divider, then the closes on the right — so the keep-vs-close split is
// obvious at a glance.
type GroupKind = "merge" | "other" | "close";
const GROUP_RANK: Record<GroupKind, number> = { merge: 0, other: 1, close: 2 };
const GROUP_LABEL: Record<GroupKind, string> = { merge: "✓ Merging", other: "Other", close: "✕ Closing" };
function dispoGroup(d: string | null | undefined): GroupKind {
  if (d === "merge") return "merge";
  if (d && d.startsWith("close")) return "close";
  return "other";
}

/** Compare several PRs' diffs as a file-aligned grid. Each changed file (the
 *  union across the selected PRs) is a full-width band, then one cell per PR
 *  showing that PR's diff of that file — or a blank when it doesn't touch it. So
 *  the same file is always horizontally aligned and the whole grid scrolls as
 *  one (no JS scroll-sync). Diffs are fetched once per PR and cached, so
 *  ticking a PR in and out is instant. */
export function DiffGrid({ prs, rows, onRemove, groupByDisposition = false }: {
  prs: number[];
  rows?: Record<number, PRRow>;
  onRemove?: (n: number) => void;
  groupByDisposition?: boolean;
}) {
  // `started` tracks which PRs we've kicked off a fetch for — it survives
  // StrictMode's mount/unmount/mount, so we never double-fetch and never strand
  // an in-flight fetch. `diffs` holds the loaded results; a PR absent from it
  // renders as "loading". Both keep growing so re-ticking a PR is instant.
  const started = useRef<Set<number>>(new Set());
  const [diffs, setDiffs] = useState<Map<number, PRDiff>>(new Map());

  useEffect(() => {
    for (const pr of prs) {
      if (started.current.has(pr)) continue;
      started.current.add(pr);
      api.diff(pr).then((d) => {
        const files = new Map<string, ParsedFile>();
        try {
          for (const f of parseDiff(d.diff || "")) files.set(f.newPath || f.oldPath || "?", f);
        } catch { /* unparseable diff → no files */ }
        setDiffs((m) => new Map(m).set(pr, { pr, loading: false, error: d.diff ? null : (d.error ?? "no diff"), files }));
      }).catch((e) => {
        setDiffs((m) => new Map(m).set(pr, { pr, loading: false, error: String(e), files: new Map() }));
      });
    }
  }, [prs.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!prs.length) {
    return <div className="dg-empty muted">Select PRs to compare their diffs side by side.</div>;
  }

  // Reorder into merge → other → close groups when asked; a stable sort keeps the
  // caller's order within each group. groupStart marks the first column of each
  // group after the first, where the vertical divider is drawn.
  const kindOf = (pr: number): GroupKind => dispoGroup(rows?.[pr]?.disposition);
  const orderedPrs = groupByDisposition
    ? [...prs].sort((a, b) => GROUP_RANK[kindOf(a)] - GROUP_RANK[kindOf(b)])
    : prs;
  const groups: { kind: GroupKind; prs: number[] }[] = [];
  if (groupByDisposition) {
    for (const pr of orderedPrs) {
      const last = groups[groups.length - 1];
      if (last && last.kind === kindOf(pr)) last.prs.push(pr);
      else groups.push({ kind: kindOf(pr), prs: [pr] });
    }
  }
  const showGroups = groupByDisposition && groups.length > 1;
  const groupStart = new Set<number>();
  if (showGroups) groups.forEach((g, i) => { if (i > 0) groupStart.add(g.prs[0]); });

  const cols: PRDiff[] = orderedPrs.map((pr) =>
    diffs.get(pr) ?? { pr, loading: true, error: null, files: new Map() });

  // union of changed files, in first-appearance order across the columns
  const fileUnion: string[] = [];
  const seen = new Set<string>();
  for (const c of cols) for (const p of c.files.keys()) if (!seen.has(p)) { seen.add(p); fileUnion.push(p); }
  const anyLoading = cols.some((c) => c.loading);

  return (
    <div className="dg" style={{ gridTemplateColumns: `repeat(${cols.length}, minmax(${MIN_COL}px, 1fr))` }}>
      {showGroups && groups.map((g) => (
        <div key={`gl-${g.kind}`}
          className={`dg-group-label dg-group-${g.kind}${groupStart.has(g.prs[0]) ? " dg-group-start" : ""}`}
          style={{ gridColumn: `span ${g.prs.length}` }}>
          {GROUP_LABEL[g.kind]} <span className="count">{g.prs.length}</span>
        </div>
      ))}
      {cols.map((c) => {
        const row = rows?.[c.pr];
        const grp = showGroups ? ` dg-group-${kindOf(c.pr)}${groupStart.has(c.pr) ? " dg-group-start" : ""}` : "";
        return (
          <div className={`dg-head${grp}`} key={`h-${c.pr}`}>
            <div className="dg-head-top">
              {row?.url
                ? <a className="mono dg-head-id" href={row.url} target="_blank" rel="noreferrer">#{c.pr} ↗</a>
                : <span className="mono dg-head-id">#{c.pr}</span>}
              {onRemove && <button className="dg-x" title="Remove this column" onClick={() => onRemove(c.pr)}>✕</button>}
            </div>
            <div className="dg-head-title" title={row?.title ?? ""}>{row?.title ?? <span className="muted">#{c.pr}</span>}</div>
            <div className="dg-head-sub muted small">
              {c.loading ? "loading…" : c.error ? c.error
                : `${c.files.size} file${c.files.size === 1 ? "" : "s"}${row?.author ? ` · @${row.author}` : ""}`}
            </div>
          </div>
        );
      })}

      {fileUnion.length === 0 && !anyLoading && (
        <div className="dg-msg muted" style={{ gridColumn: "1 / -1" }}>No file changes to show.</div>
      )}

      {fileUnion.map((path) => (
        <Fragment key={path}>
          <div className="dg-band" style={{ gridColumn: "1 / -1" }}>📄 {path}</div>
          {cols.map((c) => {
            const f = c.files.get(path);
            return (
              <div className={`dg-cell ${f ? "" : "dg-blank"}${groupStart.has(c.pr) ? " dg-group-start" : ""}`} key={`${path}::${c.pr}`}>
                {f
                  ? <Diff viewType="unified" diffType={f.type} hunks={f.hunks}>
                      {(hunks: HunkData[]) => hunks.map((h) => <Hunk key={h.content} hunk={h} />)}
                    </Diff>
                  : <span className="dg-blank-label">— not changed —</span>}
              </div>
            );
          })}
        </Fragment>
      ))}
    </div>
  );
}
