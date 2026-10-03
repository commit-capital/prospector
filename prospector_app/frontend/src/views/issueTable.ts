// The Issues table's page read, shared by the Issues view and the tab prefetch
// (prefetch.ts), so a first page read ahead lands on the key the view peeks.
import { api, type IssueFilterSpec } from "../api";
import { defineRead, type Read } from "../readCache";
import type { SortDir } from "../sortCycle";

export const ISSUE_PAGE_SIZE = 50;
export type IssueSortKey = "number" | "title" | "author" | "pain" | "repro" | "dups" | "prs" | "disposition" | "subsystem" | "fix";
export type IssueQueryResult = Awaited<ReturnType<typeof api.queryIssues>>;

export type IssueTablePage = {
  q: string;
  sortKey: IssueSortKey | "";
  sortDir: SortDir | "";
  disposition: string;
  state: string;
  fix: string;
  page: number;
  filterSpec: IssueFilterSpec;
};

/** The page the Issues view opens on when its URL names no filter. */
export const ISSUE_TABLE_FIRST_PAGE: IssueTablePage = {
  q: "", sortKey: "pain", sortDir: "desc", disposition: "", state: "open", fix: "", page: 1, filterSpec: {},
};

export function fixStatusParam(key: string): string | string[] | undefined {
  if (!key) return undefined;
  return key === "needs-you" ? ["review", "question"] : key;
}

export function issueTableRead(p: IssueTablePage): Read<IssueQueryResult> {
  const opts = {
    q: p.q,
    sort: p.sortKey || undefined,
    direction: p.sortDir || undefined,
    disposition: p.disposition || undefined,
    state: p.state === "all" ? undefined : p.state,
    fix_status: fixStatusParam(p.fix),
    collapse_dups: true,
    offset: (p.page - 1) * ISSUE_PAGE_SIZE,
    limit: ISSUE_PAGE_SIZE,
    ...p.filterSpec,
  };
  return defineRead(`issues:${JSON.stringify(opts)}`, () => api.queryIssues(opts));
}
