import type { FilterSpec } from "../api";

// Which Home column a card renders in: `act` (a click of yours moves these
// PRs), `auto` (a worker queue or hunter moves them; nothing for a person to
// do), or `handed` (the automation gave them back, to their author or to you).
export type HomeColumn = "act" | "auto" | "handed";

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
  // Sub-buckets shown as counted links under the blurb, each its own spec.
  breakdown?: HomeBreakdown[];
}

// One counted link under a card: a narrower spec inside the card's own.
export interface HomeBreakdown {
  label: string;
  spec: FilterSpec;
}

// How many sample PRs each card fetches into the table on its right side,
// kept small so every card fits above the fold; the "Show all" link opens
// the full set in the Explorer.
export const SAMPLE_LIMIT = 2;

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

// The cards, grouped by Home column, every spec a filter over the row's
// `automation` standing (prospector_app/backend/automation.py), so a card's
// number is the row count its Explorer link opens. The `act` column is what a
// click of yours moves; `auto` is what a worker queue or hunter moves on its
// own, blurbed with what clears it; `handed` is what the automation gave back,
// split by whose move it is, with a counted reason breakdown under each card.
// Counts come from POST /api/prs/counts over HOME_COUNT_SPECS and samples
// from POST /api/prs/query — the same backend matcher.
function bucket(...buckets: string[]): FilterSpec {
  return { automation_bucket: buckets.length === 1 ? buckets[0] : buckets };
}

export const HOME_CARDS: HomeCard[] = [
  {
    key: "ready",
    title: "Ready to merge",
    blurb: "Every gate is clear — review at the bar, CI passing, security GREEN, verified. One click each.",
    column: "act",
    spec: bucket("merge-ready"),
    sort: "updated",
    dir: "asc",
    lead: true,
  },
  {
    key: "approve",
    title: "Approve a parked change",
    blurb: "A resolve, fix, or description the worker prepared and parked for your approval. Approve it and the worker pushes.",
    column: "act",
    spec: bucket("approve-parked"),
  },
  {
    key: "queued",
    title: "In a queue right now",
    blurb: "Claimed or waiting in the fix or verify queue. Clears itself as the workers drain it.",
    column: "auto",
    spec: bucket("queued"),
  },
  {
    key: "hunt",
    title: "Hunter's next picks",
    blurb: "An idle worker queues these itself — a rebase, an update, a fix, a description — one attempt per head. Objection fixes wait behind the day's budget.",
    column: "auto",
    spec: bucket("hunt", "budgeted"),
  },
  {
    key: "waiting",
    title: "Waiting on a reviewer or CI",
    blurb: "The bot pushed and the reviewer or CI has not answered, a verdict is missing at this head and the worker has asked for it, or a machine failure is cooling before its retry.",
    column: "auto",
    spec: bucket("waiting", "retry"),
  },
  {
    key: "author",
    title: "Author's turn",
    blurb: "The automation cannot move these without the contributor: conflicts the bot will not rebase, red CI, verification that did not confirm the fix, or an agent that tried and declined with its reasoning.",
    column: "handed",
    spec: { automation_owner: "author" },
    breakdown: [
      { label: "needs a rebase", spec: bucket("author-conflicts") },
      { label: "red CI", spec: bucket("author-ci") },
      { label: "agent declined", spec: bucket("author-declined") },
      { label: "verification failed", spec: bucket("author-verify") },
      { label: "fix rejected by reviewer", spec: bucket("author-rejected") },
      { label: "other", spec: bucket("author-other") },
    ],
  },
  {
    key: "your-call",
    title: "Your call",
    blurb: "Handed back to you: a needs-human pick, a security verdict, the analysis's own asks, or a path the bot may not touch.",
    column: "handed",
    spec: bucket("needs-human", "security-red", "security-yellow", "asks", "gated", "other"),
    breakdown: [
      { label: "needs a human decision", spec: bucket("needs-human") },
      { label: "security RED", spec: bucket("security-red") },
      { label: "security YELLOW", spec: bucket("security-yellow") },
      { label: "analysis asks", spec: bucket("asks") },
      { label: "gated path", spec: bucket("gated") },
      { label: "other", spec: bucket("other") },
    ],
  },
];

// Every breakdown entry, flattened in card order, with the card it belongs to.
export const HOME_BREAKDOWN_ENTRIES: { cardKey: string; entry: HomeBreakdown }[] =
  HOME_CARDS.flatMap((c) => (c.breakdown ?? []).map((entry) => ({ cardKey: c.key, entry })));

// The one counts request: the cards' specs first, then every breakdown
// entry's, so a card's count is at its HOME_CARDS index and a breakdown's at
// HOME_CARDS.length + its HOME_BREAKDOWN_ENTRIES index.
export const HOME_COUNT_SPECS: FilterSpec[] = [
  ...HOME_CARDS.map((c) => c.spec),
  ...HOME_BREAKDOWN_ENTRIES.map((e) => e.entry.spec),
];

// The Explorer link for a breakdown entry: the card's sort, the entry's spec.
export function breakdownHref(card: HomeCard, entry: HomeBreakdown): string {
  return exploreHref({ ...card, spec: entry.spec });
}

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
