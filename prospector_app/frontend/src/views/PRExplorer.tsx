import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { api, type FilterSpec, type PRRow, type QueryResult, type DeepResult } from "../api";
import type { CellCtx, ColumnDef } from "../components/explorer/columns";
import { reuseRows } from "../components/explorer/rowReuse";
import { LaneChips } from "../components/explorer/LaneChips";
import { FilterControls } from "../components/explorer/FilterControls";
import { ColumnToggles } from "../components/explorer/ColumnToggles";
import { ExplorerSearchBar } from "../components/explorer/ExplorerSearchBar";
import { BulkActionBar, type BulkAction } from "../components/explorer/BulkActionBar";
import { BulkConfirmDialog } from "../components/explorer/BulkConfirmDialog";
import { FilterSummary } from "../components/shared/FilterSummary";
import { buildPrFilterParts } from "../components/explorer/prFilterParts";
import { ColumnFilterPopout, FILTERABLE_COLS, isColFilterActive } from "../components/explorer/ColumnFilterPopout";
import { InfoTip, Term } from "../components/InfoTip";
import { SuggestedActions } from "../components/SuggestedActions";
import { term } from "../glossary";
import { usePRFlyout } from "../usePRFlyout";
import { makeRowOpen, stopRowOpen } from "../rowOpen";
import { useColumnPrefs } from "../useColumnPrefs";
import { useExec } from "../ExecContext";
import { useAgentPane } from "../components/AgentPane";
import { cycleSort, type SortDir } from "../sortCycle";

const SPEC_PARAM = "spec";
const Q_PARAM = "q";
type PageSizeOption = 50 | 100 | 200 | "all";
const PAGE_SIZE_OPTIONS: PageSizeOption[] = [50, 100, 200, "all"];
const PAGE_SIZE_KEY = "app-explorer-page-size";
// Exceeds the open-PR corpus (~3k) and the backend's MAX_PR_QUERY_LIMIT, so
// "All" always returns every matching row in a single page.
const ALL_ROWS_LIMIT = 5000;

function readPageSize(): PageSizeOption {
  const saved = localStorage.getItem(PAGE_SIZE_KEY);
  if (saved === "all") return "all";
  const n = Number(saved);
  return n === 50 || n === 100 || n === 200 ? n : 50;
}

function queryCacheKey(specKey: string, sortKey: string, dir: string, page: number, pageSize: PageSizeOption, deepKey: string): string {
  return JSON.stringify([specKey, sortKey, dir, page, pageSize, deepKey]);
}

// Module-scoped (not component state) so it survives a PRExplorer remount within
// the same page load — e.g. navigating away and back — letting the next mount
// show the last-seen result instantly instead of the ~30s blocking reload, while
// still revalidating in the background. Only the most recent query is kept.
let lastQuery: { key: string; result: QueryResult } | null = null;

// Columns whose first click sorts descending — quality/size/recency. Everything
// else (text columns: title, author, cluster, disposition, drift, summary) leads
// ascending. Mirrors the backend `_DEFAULT_DESC` so the caret matches the order.
const DESC_FIRST = new Set([
  "pr", "greptile", "review", "scans", "safety", "updated", "loc", "files",
  "checks", "merge", "age", "author_rate", "pain", "issues",
]);

function readSpec(params: URLSearchParams): FilterSpec {
  try { return JSON.parse(params.get(SPEC_PARAM) || "{}"); } catch { return {}; }
}

// One PR row, memoized. The table can hold thousands of rows (page size
// "All"), and rendering them all costs seconds — a row re-renders only when
// one of its own inputs changes, keeping Explorer-wide state changes (an open
// filter popout, a search keystroke, another row's selection) off the rows.
// Every prop is identity-stable across such renders: `r` survives refetches
// via reuseRows, `visibleColumns` is memoized in useColumnPrefs, and the
// callbacks are stable by construction in PRExplorer. Row-open clicks are
// delegated to the <tbody> (via `data-pr`), so the row carries no URL-derived
// handlers of its own.
const PrRow = memo(function PrRow({ r, visibleColumns, isSelected, isOpen, cellCtx, onToggle }: {
  r: PRRow;
  visibleColumns: ColumnDef[];
  isSelected: boolean;
  isOpen: boolean;
  cellCtx: CellCtx;
  onToggle: (n: number) => void;
}) {
  return (
    <tr data-pr={r.number}
      className={`rowlink ${isSelected ? "sel" : ""} ${isOpen ? "row-open" : ""}`}>
      <td onClick={stopRowOpen}>
        <input type="checkbox" checked={isSelected} onChange={() => onToggle(r.number)} />
      </td>
      {visibleColumns.map((col) => (
        <td key={col.key} className={col.cellClass} onClick={col.stopOpen ? stopRowOpen : undefined}>
          {col.cell(r, cellCtx)}
        </td>
      ))}
    </tr>
  );
});

export default function PRExplorer() {
  const [params, setParams] = useSearchParams();
  const spec = useMemo(() => readSpec(params), [params]);
  const searchQ = params.get(Q_PARAM) ?? "";
  const { openPR, addPane, prs: openPrs } = usePRFlyout();
  // Row-open clicks are delegated to the <tbody>: openPR/addPane re-derive
  // from the URL on every search-param change, and the delegated handler reads
  // the current pair at click time while the memoized rows carry no
  // URL-derived props. Interactive cells stop propagation (stopRowOpen),
  // which suppresses the delegated handler just like a row-level one.
  const rowOpen = makeRowOpen(openPR, addPane);
  const rowFromEvent = (e: React.MouseEvent<HTMLTableSectionElement>): number | null => {
    const tr = (e.target as HTMLElement).closest("tr[data-pr]");
    if (!(tr instanceof HTMLTableRowElement) || !tr.dataset.pr) return null;
    return Number(tr.dataset.pr);
  };
  const onRowClick = (e: React.MouseEvent<HTMLTableSectionElement>) => {
    const n = rowFromEvent(e);
    if (n !== null) rowOpen(n).onClick(e);
  };
  const onRowAuxClick = (e: React.MouseEvent<HTMLTableSectionElement>) => {
    const n = rowFromEvent(e);
    if (n !== null) rowOpen(n).onAuxClick(e);
  };
  const { isOn: colOn, toggle: toggleCol, reset: resetCols, visibleColumns } = useColumnPrefs();
  const { setVisiblePrs } = useAgentPane();
  const [page, setPage] = useState(1);
  const [pageSize, setPageSizeState] = useState<PageSizeOption>(readPageSize);
  // A fresh mount always starts on page 1 with no deep-search overlay (that's
  // transient UI state, declared below), so those are the initial-render values
  // to check the cache against — matching the effect's own key below.
  const initialCached = lastQuery?.key === queryCacheKey(params.get(SPEC_PARAM) ?? "", params.get("sort") || "", params.get("dir") || "", 1, pageSize, "")
    ? lastQuery.result : null;
  const [res, setRes] = useState<QueryResult | null>(initialCached);
  const [loading, setLoading] = useState(initialCached === null);
  // Persist the operator's preferred page size across reloads; private-mode safe.
  useEffect(() => {
    try { localStorage.setItem(PAGE_SIZE_KEY, String(pageSize)); } catch { /* private mode: keep in-memory */ }
  }, [pageSize]);
  const setPageSize = (v: PageSizeOption) => { setPageSizeState(v); setPage(1); };
  const effectivePageSize = pageSize === "all" ? ALL_ROWS_LIMIT : pageSize;
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [pending, setPending] = useState<{ action: BulkAction; comment: string; canonical?: number; perPr: boolean; reviewer?: string } | null>(null);
  const { reviewers } = useExec();
  const reviewerLabels = useMemo(
    () => Object.fromEntries(reviewers.map((r) => [r.id, r.label])), [reviewers]);
  // Deep Search overlay: an agent-judged result set over the current filters.
  const [deep, setDeep] = useState<{ query: string; result: DeepResult } | null>(null);
  const [deepBusy, setDeepBusy] = useState(false);
  const [deepProgress, setDeepProgress] = useState<{ done: number; total: number } | null>(null);
  // Which column's filter popout is open, and where to anchor it.
  const [openFilter, setOpenFilter] = useState<{ key: string; rect: DOMRect } | null>(null);

  const setSpec = (next: FilterSpec) => {
    const p = new URLSearchParams(params);
    if (Object.keys(next).length) p.set(SPEC_PARAM, JSON.stringify(next));
    else p.delete(SPEC_PARAM);
    p.delete("pr"); // a new query closes any open flyout from the prior result set
    setParams(p);
    setPage(1); // a new query starts over at the first page
    setDeep(null); // a new filter/search invalidates the deep-search overlay
  };

  // Keep the search bar text in the URL (live, replace-mode to avoid history spam)
  // so the view is shareable — loading the URL restores the search bar and the
  // applied spec together.
  const setSearchText = (text: string) => {
    const p = new URLSearchParams(params);
    if (text) p.set(Q_PARAM, text);
    else p.delete(Q_PARAM);
    setParams(p, { replace: true });
  };

  // sort — kept in the URL so it survives a refresh and round-trips through the
  // query engine. First click on a column = its primary direction, second flips
  // it, third clears the sort. The direction is always written explicitly so the
  // header caret matches what the engine returns: quality/size columns lead with
  // descending (biggest/best first), text columns with ascending (A→Z). This set
  // mirrors the backend's `_DEFAULT_DESC` (service.py).
  const sortKey = params.get("sort") || "";
  const dir = (params.get("dir") || "") as SortDir | "";
  const sortByCol = (col: string) => {
    const next = cycleSort({ key: sortKey, dir }, col, DESC_FIRST);
    const p = new URLSearchParams(params);
    if (next.key) { p.set("sort", next.key); p.set("dir", next.dir); } else { p.delete("sort"); p.delete("dir"); }
    setParams(p);
    setPage(1); // re-sorting reorders the whole result set, so start at the first page
  };
  const sortArrow = (col: string) => sortKey === col ? (dir === "asc" ? " ▲" : " ▼") : "";

  // Deep Search overlays its agent-judged PR-number set onto the engine via the
  // transient `numbers` filter, so sorting + pagination still come from the
  // server (and the URL stays the user's filters, not a giant id list).
  const deepIds = useMemo(() => deep ? deep.result.matches.map((m) => m.pr) : null, [deep]);
  const deepReasons = useMemo(
    () => new Map((deep?.result.matches ?? []).map((m) => [m.pr, m.reason])), [deep]);
  const effSpec: FilterSpec = deepIds ? { ...spec, numbers: deepIds } : spec;
  const deepKey = deepIds ? deepIds.join(",") : "";

  // refetch when the filter spec, sort, page, or deep overlay changes (not when
  // the ?pr flyout opens)
  const specKey = params.get(SPEC_PARAM) ?? "";
  const queryOpts = {
    sort: sortKey || undefined, direction: dir || undefined,
    offset: (page - 1) * effectivePageSize, limit: effectivePageSize,
  };
  // The latest result, readable outside the render cycle: reuseRows folds each
  // fresh fetch onto whatever is currently shown, so unchanged rows keep their
  // object identity and their memoized <PrRow> skips re-rendering.
  const resRef = useRef(res);
  useEffect(() => { resRef.current = res; }, [res]);
  useEffect(() => {
    const key = queryCacheKey(specKey, sortKey, dir, page, pageSize, deepKey);
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- mark loading before issuing the query
    setLoading(true);
    api.queryPrs(effSpec, queryOpts).then((r) => {
      if (cancelled) return; // superseded by a newer query (filter/sort/page changed again)
      const merged = reuseRows(resRef.current, r);
      lastQuery = { key, result: merged };
      setRes(merged);
      setLoading(false);
    }).catch(() => {
      // Leave any already-visible data in place — a failed refresh shouldn't
      // wipe the table, just stop spinning so the operator isn't stuck waiting.
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [specKey, sortKey, dir, page, pageSize, deepKey]);

  const refetch = useCallback(() => api.queryPrs(effSpec, queryOpts).then((r) => {
    const merged = reuseRows(resRef.current, r);
    lastQuery = { key: queryCacheKey(specKey, sortKey, dir, page, pageSize, deepKey), result: merged };
    setRes(merged);
    // effSpec/queryOpts are rebuilt each render but fully determined by these keys
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [specKey, sortKey, dir, page, pageSize, deepKey]);

  // Let the agent pane see the same filtered set the operator is looking at, so
  // "review these" doesn't need the PR numbers spelled out (#355). Every matching
  // PR, not just the current page — same list "select all matching" uses below.
  // Cleared on unmount so a stale list doesn't leak into chat on another page.
  useEffect(() => {
    setVisiblePrs(res?.match_ids ?? null);
    return () => setVisiblePrs(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [res]);

  const rows = useMemo(() => res?.items ?? [], [res]);
  const matchIds = res?.match_ids ?? [];
  const pageIds = rows.map((r: PRRow) => r.number);
  // The header checkbox scopes to the current page; "Select all N matching" escalates across pages.
  const allSelected = matchIds.length > 0 && matchIds.every((n) => selected.has(n));
  const pageSelected = pageIds.length > 0 && pageIds.every((n) => selected.has(n));
  const pageSomeSelected = pageIds.some((n) => selected.has(n));
  // Identity-stable (functional updater, no captured state) — it is a prop of
  // every memoized row.
  const toggle = useCallback((n: number) => setSelected((s) => {
    const next = new Set(s);
    if (next.has(n)) next.delete(n); else next.add(n);
    return next;
  }), []);
  const setPageSelected = (on: boolean) => setSelected((s) => {
    const next = new Set(s);
    for (const n of pageIds) { if (on) next.add(n); else next.delete(n); }
    return next;
  });
  const selectAllMatching = () => setSelected(new Set(matchIds));
  const clearSel = () => setSelected(new Set());

  // Deep Search: agent-judge the current match set against the query text.
  const runDeep = async (query: string) => {
    if (!query.trim() || deepBusy) return;
    const candidates = res?.match_ids ?? [];
    if (!candidates.length) return;
    setDeepBusy(true);
    setDeepProgress({ done: 0, total: candidates.length });
    try {
      const result = await api.deepSearch(query, candidates, setDeepProgress);
      setDeep({ query, result });
      setSelected(new Set()); // the prior selection was over the un-judged set
      setPage(1);
    } finally {
      setDeepBusy(false);
      setDeepProgress(null);
    }
  };
  const clearDeep = () => { setDeep(null); setPage(1); };

  const totalPages = res ? Math.max(1, Math.ceil(res.total / effectivePageSize)) : 1;
  const hasPrev = page > 1;
  const hasNext = res ? res.offset + rows.length < res.total : false;
  const goToPage = (p: number) => {
    setPage(p);
    window.scrollTo({ top: 0 });
  };

  const ackResponse = useCallback(async (pr: number) => {
    await api.ackResponse(pr);
    await refetch();
  }, [refetch]);
  // Memoized for the same reason as `toggle`: its identity is a prop of every
  // memoized row.
  const cellCtx: CellCtx = useMemo(
    () => ({ deepReasons, ackResponse }), [deepReasons, ackResponse]);
  return (
    <div className="pr-explorer">
      <div className="explorer-head">
        <h2>PR Explorer</h2>
        <span className="muted">
          {loading && res !== null && <span className="spinner" style={{ marginRight: 6 }} />}
          {res ? `${res.total} match` : null}
        </span>
      </div>
      <SuggestedActions view="prs" />
      <ExplorerSearchBar value={searchQ} onTextChange={setSearchText} onSpec={setSpec} onDeepSearch={runDeep} deepBusy={deepBusy} deepProgress={deepProgress} />
      <LaneChips spec={spec} onChange={setSpec} />
      <FilterControls spec={spec} onChange={setSpec} visibleColKeys={new Set(visibleColumns.map((c) => c.key))} />
      <ColumnToggles isOn={colOn} toggle={toggleCol} reset={resetCols} />
      <FilterSummary parts={buildPrFilterParts(spec, setSpec, reviewerLabels)} total={res?.total ?? null} />
      {deep && (
        <div className="deep-banner">
          <span className="deep-msg">
            🪄 Deep search <b>“{deep.query}”</b> — <b>{deep.result.matches.length}</b> matched of {deep.result.total} judged
            {deep.result.from_cache > 0 && <span className="muted small"> · {deep.result.from_cache} reused</span>}
          </span>
          {deep.result.capped && (
            <span className="chip chip-yellow sm" title={`Hit the ${deep.result.total}-PR ceiling — narrow with filters first to judge more.`}>capped</span>
          )}
          <InfoTip entry={term("deep-search")} cue={false}><span className="chip chip-blue sm">agent-judged</span></InfoTip>
          <button className="link-btn" onClick={clearDeep}>clear</button>
        </div>
      )}

      <BulkActionBar selected={[...selected]}
        onApply={(args) => setPending(args)} />

      {selected.size > 0 && matchIds.length > rows.length && !allSelected && (
        <div className="select-all-banner">
          <span>{pageSelected ? `All ${pageIds.length} PRs on this page are selected.` : `${selected.size} selected.`}</span>
          <button className="link-btn" onClick={selectAllMatching}>
            Select all {matchIds.length} matching →
          </button>
        </div>
      )}

      {loading && res === null && (
        <div className="explorer-loading">
          <span className="spinner explorer-loading-spinner" />
          <span className="explorer-loading-label">Loading PRs…</span>
        </div>
      )}

      {openFilter && (
        <ColumnFilterPopout
          colKey={openFilter.key}
          spec={spec}
          onChange={setSpec}
          rect={openFilter.rect}
          onClose={() => setOpenFilter(null)}
        />
      )}

      <table className="grid sortable pr-table" style={loading && res === null ? { display: "none" } : undefined}>
        <thead><tr>
          <th><input type="checkbox" checked={pageSelected}
            ref={(el) => { if (el) el.indeterminate = pageSomeSelected && !pageSelected; }}
            onChange={() => allSelected ? clearSel() : setPageSelected(!pageSelected)} /></th>
          {/* every column is server-sortable (#180) — the sort spans all pages */}
          {visibleColumns.map((col) => {
            const canFilter = FILTERABLE_COLS.has(col.key);
            const filterActive = isColFilterActive(col.key, spec, reviewers);
            return (
              <th key={col.key}
                className={[col.numeric ? "num" : "", "sortable-th", sortKey === col.key ? "sorted" : ""].filter(Boolean).join(" ")}
                onClick={() => sortByCol(col.key)}
                aria-sort={sortKey === col.key ? (dir === "asc" ? "ascending" : "descending") : "none"}
                title={col.key === "updated" ? "When the PR last changed upstream. Click to sort by recency. Chips show how the author responded since we acted: ↩ reopened · ⬆ new commits · 💬 replied (scan from the Control tab)." : "Click to sort by this column"}>
                <span className="th-inner">
                  <span className="th-label">
                    {col.term ? <Term k={col.term} cue={false}>{col.label}</Term> : col.label}
                    {sortArrow(col.key)}
                  </span>
                  {canFilter && (
                    <button
                      className={`col-filter-btn${filterActive ? " col-filter-active" : ""}`}
                      title={`Filter by ${col.label}${filterActive ? " (active)" : ""}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
                        setOpenFilter(openFilter?.key === col.key ? null : { key: col.key, rect });
                      }}
                    >
                      ▾
                    </button>
                  )}
                </span>
              </th>
            );
          })}
        </tr></thead>
        <tbody onClick={onRowClick} onAuxClick={onRowAuxClick}>
          {rows.map((r: PRRow) => (
            <PrRow key={r.number} r={r} visibleColumns={visibleColumns}
              isSelected={selected.has(r.number)} isOpen={openPrs.includes(r.number)}
              cellCtx={cellCtx} onToggle={toggle} />
          ))}
        </tbody>
      </table>

      <div className="pager">
        <label className="page-size-select muted small">
          Rows per page:{" "}
          <select value={pageSize}
            onChange={(e) => setPageSize((e.target.value === "all" ? "all" : Number(e.target.value)) as PageSizeOption)}>
            {PAGE_SIZE_OPTIONS.map((opt) => (
              <option key={opt} value={opt}>{opt === "all" ? "All" : opt}</option>
            ))}
          </select>
        </label>
        {totalPages > 1 && (
          <>
            <button disabled={!hasPrev} onClick={() => goToPage(page - 1)}>‹ Prev</button>
            <span className="muted small">Page {page} of {totalPages}</span>
            <button disabled={!hasNext} onClick={() => goToPage(page + 1)}>Next ›</button>
          </>
        )}
      </div>

      {pending && (
        <BulkConfirmDialog {...pending} selected={[...selected]} rows={rows}
          onClose={() => setPending(null)}
          onDone={() => { clearSel(); refetch(); }}
          onJobsFinished={refetch} />
      )}
    </div>
  );
}
