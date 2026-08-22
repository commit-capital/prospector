import type { CheckClause, FilterSpec } from "../api";
// Explicit .ts extension so the node:test runner (type stripping, no bundler)
// can resolve this runtime import when homeCards.test.ts loads the module.
import { MERGE_READY_SPEC } from "../components/explorer/lanes.ts";

// Which part of the Home layout a card renders in: the merge track column
// (the pipeline's merge picks), the request-changes column beside it, or the
// full-width human-decision backstop beneath the columns.
export type HomeColumn = "merge" | "changes" | "backstop";

// A per-sample-row job button on a PR card: the Control-tab job to run for
// that row's PR, with the button's label and hover hint.
export interface HomeRowAction {
  kind: "verify-pr" | "security-review";
  label: string;
  hint: string;
}

// One Home card: a headline count over a filter spec, linking to the PR
// Explorer with that spec (plus an optional sort) in the URL. `rowAction`
// puts a run button on each sample row when a per-PR job is what moves the
// card's PRs forward.
export interface HomeCard {
  key: string;
  title: string;
  blurb: string;
  column: HomeColumn;
  spec: FilterSpec;
  sort?: string;
  dir?: "asc" | "desc";
  lead?: boolean;
  rowAction?: HomeRowAction;
}

// How many sample PRs each card fetches into the table on its right side;
// the "Show all" link opens the full set in the Explorer.
export const SAMPLE_LIMIT = 6;

// How many linked issues a sample row lists before collapsing the rest into a
// "+N" marker — the issues cell is a single fixed-width line sized to fit this
// many links.
export const SAMPLE_ISSUE_LIMIT = 2;

// The Community Pain Score cell label for a sample PR row; empty when the
// PR carries no pain score.
export function painLabel(pain: number | null | undefined): string {
  return pain ? `🔥 ${pain.toFixed(2)}` : "";
}

// The query options behind each card's inline sample: its highest-pain PRs
// first, so the card leads with the members the community is waiting on.
export const SAMPLE_QUERY: { sort: string; direction: "desc"; limit: number } = {
  sort: "pain",
  direction: "desc",
  limit: SAMPLE_LIMIT,
};

function checksPass(...keys: string[]): CheckClause[] {
  return keys.map((key) => ({ key, status: "pass" }));
}

// The cards, grouped by Home column. The merge column holds only the
// pipeline's merge picks — every spec carries disposition "merge", so a PR the
// pipeline decided to close as a dup or send back for changes never appears —
// ordered by how close each card's PRs are to merge-ready: ready first, then
// each card one step further from the finish line. The changes column holds
// the request-changes picks whose CI is already clean — asks worth relaying to
// their authors now. The human-decision backstop renders full-width beneath
// both. Counts come from POST /api/prs/counts and samples from
// POST /api/prs/query — the same backend matcher — so each card's number is
// exactly the row count the Explorer shows when its link opens.
export const HOME_CARDS: HomeCard[] = [
  {
    key: "ready",
    title: "PRs ready to merge",
    blurb: "A pipeline merge pick with every check green — review at the bar, CI passing, security GREEN, verified. Just merge.",
    column: "merge",
    // The Explorer's Merge-ready lane spec, narrowed to the pipeline's merge picks.
    spec: { ...MERGE_READY_SPEC, disposition: "merge" },
    sort: "updated",
    dir: "asc",
    lead: true,
  },
  {
    key: "verify-pending",
    title: "Awaiting verification",
    blurb: "Security GREEN and otherwise clean, but dynamic verification has never run.",
    column: "merge",
    spec: {
      checks: [
        ...checksPass("review", "ci", "scans", "mergeable", "secrets", "security"),
        { key: "verify", status: "never_ran" },
      ],
      safety: "GREEN",
      disposition: "merge",
    },
    rowAction: {
      kind: "verify-pr",
      label: "verify",
      hint: "Run sandbox verification for this PR — needs the Docker sandbox",
    },
  },
  {
    key: "security-pending",
    title: "Awaiting security review",
    blurb: "Clean on review, CI, conflicts, and secrets, but the deep security review has never run.",
    column: "merge",
    spec: {
      checks: [
        ...checksPass("review", "ci", "scans", "mergeable", "secrets"),
        { key: "security", status: "never_ran" },
      ],
      disposition: "merge",
    },
    rowAction: {
      kind: "security-review",
      label: "review",
      hint: "Run the deep security review for this PR",
    },
  },
  {
    key: "base-update",
    title: "Just need a base update",
    blurb: "Green everywhere except merge conflicts — update the branch with the base and they can go fully green.",
    column: "merge",
    spec: {
      checks: [
        ...checksPass("review", "ci", "scans", "tests", "secrets", "security", "verify"),
        { key: "mergeable", status: "fail" },
      ],
      safety: "GREEN",
      disposition: "merge",
    },
  },
  {
    key: "changes",
    title: "Changes to request",
    blurb: "CI passing and no conflicts, but the pipeline's pick is request-changes — relay the asks to the authors.",
    column: "changes",
    spec: {
      checks: checksPass("ci", "mergeable"),
      disposition: "request-changes",
    },
    lead: true,
  },
  {
    key: "nitpicks",
    // The subset of the changes column whose review feedback is only nits —
    // the cheapest asks to relay.
    title: "Just need nitpicks fixed",
    blurb: "CI passing, no conflicts, but the review left nits below the bar — a nitpick pass unblocks them.",
    column: "changes",
    spec: {
      checks: checksPass("ci", "mergeable"),
      greptile_severity: "nits",
      disposition: "request-changes",
    },
  },
  {
    key: "needs-human",
    title: "Need a human decision",
    blurb: "Flagged needs-human — only an operator call moves these forward.",
    column: "backstop",
    spec: { disposition: "needs-human" },
  },
];

export function exploreHref(card: HomeCard): string {
  const params = new URLSearchParams();
  params.set("spec", JSON.stringify(card.spec));
  if (card.sort) params.set("sort", card.sort);
  if (card.dir) params.set("dir", card.dir);
  return `/explore?${params}`;
}

// The card-level job button on a Home issue card: the Control-tab job that
// moves the card's issues forward, run with a count capped at `batch`.
export interface HomeIssueAction {
  kind: string;
  label: string;
  batch: number;
}

// The Analyze button's batch cap, matching the Control tab's issue-analyze
// default.
export const ISSUE_ANALYZE_BATCH = 200;

// One Home issue card: a headline count over an Issues-view disposition
// filter, linking there with that filter in the URL. `disposition` uses the
// issues query API's vocabulary, where "none" selects unanalyzed issues.
export interface HomeIssueCard {
  key: string;
  title: string;
  blurb: string;
  disposition: "close-fixed" | "none";
  action?: HomeIssueAction;
  lead?: boolean;
}

// The Issues column's cards: open issues whose triage pick is ready to act on,
// and the backlog the issue pipeline has yet to look at. Counts and samples
// come from POST /api/issues/query — the same matcher behind the Issues table —
// so each card's number is exactly the row count its link opens.
export const HOME_ISSUE_CARDS: HomeIssueCard[] = [
  {
    key: "issues-close-fixed",
    title: "Issues to close as fixed",
    blurb: "The triage pick is close-fixed — a merged PR already fixed each one. Review and close them as the bot.",
    disposition: "close-fixed",
    lead: true,
  },
  {
    key: "issues-unanalyzed",
    title: "Unanalyzed issues",
    blurb: "Open issues the issue pipeline has never analyzed — run analysis to give each a triage pick.",
    disposition: "none",
    action: { kind: "issue-analyze", label: "Analyze", batch: ISSUE_ANALYZE_BATCH },
  },
];

export function issuesHref(card: HomeIssueCard): string {
  return `/issues?disposition=${encodeURIComponent(card.disposition)}`;
}
