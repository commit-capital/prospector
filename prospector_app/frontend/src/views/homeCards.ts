import type { FilterSpec } from "../api";

// Which Home column a card renders in: `act` (a click of yours moves these
// PRs), `auto` (a worker queue or hunter moves them; nothing for a person to
// do), or `handed` (the automation gave them back, to their author or to you).
export type HomeColumn = "act" | "auto" | "handed";

// Home's columns left to right, under a labeled group each: the issue cards
// lead, then the PR columns.
export type HomeSection = "issues" | HomeColumn;
export interface HomeGroup {
  key: "issues" | "prs";
  label: string;
  sections: readonly HomeSection[];
}
export const HOME_GROUPS: readonly HomeGroup[] = [
  { key: "issues", label: "Issues", sections: ["issues"] },
  { key: "prs", label: "PRs", sections: ["act", "auto", "handed"] },
];

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
  // Styled as an alarm (red), for PRs that are a threat rather than work.
  alert?: boolean;
  // Rendered only while its count is above zero.
  hideWhenEmpty?: boolean;
  rowAction?: HomeRowAction;
  // Each sample row says how its author answered us (PRRow.author_return, or
  // the design answer in its automation reason), with its automation standing.
  cameBack?: boolean;
  // Sub-buckets shown as counted links under the blurb, each its own spec.
  breakdown?: HomeBreakdown[];
}

// One counted link under a card: a narrower spec inside the card's own.
// `hint` says what would have let the agent act alone on these PRs.
export interface HomeBreakdown {
  label: string;
  spec: FilterSpec;
  hint?: string;
}

// What would have let the agent act alone, per handed bucket — every
// escalation names its reason (the row's automation.reason), and this is the
// reason's counterpart: the missing condition that would have kept the PR in
// the automation's hands.
export const HANDED_UNBLOCK: Record<string, string> = {
  "author-conflicts": "A head that rebases or merges cleanly — conflicts are the author's (or a hunted resolve under TRIAGE_FIX_HUNT_RESOLVE).",
  "author-ci": "CI green at the author's head — the bot only repairs CI it broke itself.",
  "author-declined": "Nothing — the agent judged the change unsafe to write and said why.",
  "author-verify": "A verification run that confirms the fix red→green.",
  "author-rejected": "A patch the refuting reviewer calls safe.",
  "author-design": "An author push that addresses the design findings posted on the PR.",
  "author-other": "An author push that clears the recorded refusal.",
  "stacked": "Its base PR merging, so GitHub retargets it onto the default branch.",
  "needs-human": "Nothing — a needs-human pick is a decision reserved for a person.",
  "security-red": "Nothing — a RED security verdict always stops the automation.",
  "security-yellow": "TRIAGE_FIX_HUNT_SECURITY=1, which lets the hunter fix YELLOW findings as objections.",
  "asks": "A profile naming the gate fixable, or your typed guidance for the fix.",
  "gated": "Paths outside CODEOWNERS gating and the profile's deny globs — the bot may not author there.",
  "other": "Depends on the row's own reason — open it to see what stands in the way.",
};

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
    key: "malicious",
    title: "⛔ Flagged malicious",
    blurb: "The threat scan matched a supply-chain attack pattern or a blocked author. These can never merge — close each one on GitHub and check the author's other work.",
    column: "act",
    spec: bucket("malicious"),
    alert: true,
    hideWhenEmpty: true,
  },
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
    key: "approve-ci",
    title: "Approve or re-run CI",
    blurb: "A first-time contributor's CI waits on a maintainer's approval, or CI fails only on checks that ran before the PR description's last edit, which re-runs nothing. Approve or re-run the checks on the PR's Checks tab on GitHub.",
    column: "act",
    spec: bucket("approve-ci", "rerun-ci"),
    hideWhenEmpty: true,
  },
  {
    key: "came-back",
    title: "Authors answered us",
    blurb: "The author answered what we decided: reopened a PR we closed, opened this one linking the PR or issue we closed, or replied or pushed after we posted design findings. It's our move — read their side and move each to its next step.",
    column: "act",
    spec: { author_return: true },
    sort: "updated",
    dir: "desc",
    cameBack: true,
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
    blurb: "The bot pushed and the reviewer or CI has not answered, a verdict is missing at this head and the worker has asked for it, a machine failure is cooling before its retry, or Prospector's own fix PR is being followed up.",
    column: "auto",
    spec: bucket("waiting", "retry", "followup"),
  },
  {
    key: "author",
    title: "Someone else's turn",
    blurb: "The automation cannot move these without the contributor: conflicts the bot will not rebase, red CI, verification that did not confirm the fix, design findings posted for them to address, an agent that tried and declined with its reasoning, or a stacked PR waiting on its base.",
    column: "handed",
    spec: { automation_owner: "author" },
    breakdown: [
      { label: "needs a rebase", spec: bucket("author-conflicts"), hint: HANDED_UNBLOCK["author-conflicts"] },
      { label: "red CI", spec: bucket("author-ci"), hint: HANDED_UNBLOCK["author-ci"] },
      { label: "agent declined", spec: bucket("author-declined"), hint: HANDED_UNBLOCK["author-declined"] },
      { label: "verification failed", spec: bucket("author-verify"), hint: HANDED_UNBLOCK["author-verify"] },
      { label: "fix rejected by reviewer", spec: bucket("author-rejected"), hint: HANDED_UNBLOCK["author-rejected"] },
      { label: "design findings posted", spec: bucket("author-design"), hint: HANDED_UNBLOCK["author-design"] },
      { label: "stacked on another PR", spec: bucket("stacked"), hint: HANDED_UNBLOCK["stacked"] },
      { label: "other", spec: bucket("author-other"), hint: HANDED_UNBLOCK["author-other"] },
    ],
  },
  {
    key: "your-call",
    title: "Your call",
    blurb: "Handed back to you: a needs-human pick, a security verdict, the analysis's own asks, a path the bot may not touch, or Prospector's own fix PR that its follow-up gave up on.",
    column: "handed",
    spec: bucket("needs-human", "security-red", "security-yellow", "asks", "gated", "other", "handed-back"),
    breakdown: [
      { label: "needs a human decision", spec: bucket("needs-human"), hint: HANDED_UNBLOCK["needs-human"] },
      { label: "security RED", spec: bucket("security-red"), hint: HANDED_UNBLOCK["security-red"] },
      { label: "security YELLOW", spec: bucket("security-yellow"), hint: HANDED_UNBLOCK["security-yellow"] },
      { label: "analysis asks", spec: bucket("asks"), hint: HANDED_UNBLOCK["asks"] },
      { label: "gated path", spec: bucket("gated"), hint: HANDED_UNBLOCK["gated"] },
      { label: "other", spec: bucket("other"), hint: HANDED_UNBLOCK["other"] },
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
  return `/prs/list?${params}`;
}

// The Security card under "Your move": open critical/high advisories the
// find-fixed pass still marks not-fixed, plus every open secret-scanning
// alert. It reads the advisory and alert stores, not the PR matcher, so its
// two sides are query options for POST /api/advisories/query and
// POST /api/alerts/query rather than a FilterSpec.
export const SECURITY_CARD = {
  key: "security",
  title: "Security needs a look",
  blurb: "Critical or high advisories the find-fixed pass still marks not-fixed, plus any open secret-scanning alert. Triage each in the Security views.",
  href: "/security",
};

// The advisory side: open (triage/draft) critical or high reports whose
// fix-scan verdict is not-fixed, most severe first, ties oldest first.
export const SECURITY_ADVISORY_QUERY: {
  state: string[]; severity: string[]; verdict: string; sort: string; limit: number;
} = {
  state: ["triage", "draft"],
  severity: ["critical", "high"],
  verdict: "not-fixed",
  sort: "severity",
  limit: SAMPLE_LIMIT,
};
export const SECURITY_ADVISORIES_HREF = "/security?security=advisories";

// The alert side: every open secret-scanning alert, most severe first.
export const SECURITY_ALERT_QUERY: {
  source: string; state: string; sort: string; limit: number;
} = {
  source: "secret-scanning",
  state: "open",
  sort: "severity",
  limit: SAMPLE_LIMIT,
};
export const SECURITY_ALERTS_HREF = "/security?security=alerts";

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

// One Home issue card: a headline count over an Issues-view filter, linking
// there with that filter in the URL — a triage `disposition` (the issues query
// API's vocabulary, where "none" selects unanalyzed issues) or an auto-fix
// `fix` status (fix_review.STATUSES).
export interface HomeIssueCard {
  key: string;
  title: string;
  blurb: string;
  disposition?: "close-fixed" | "none";
  fix?: "review" | "question";
  // The open issues an author came back to after our close (author_returns).
  cameBack?: boolean;
  action?: HomeIssueAction;
  lead?: boolean;
}

// The Issues column's cards: open issues whose triage pick is ready to act on,
// and the backlog the issue pipeline has yet to look at. Counts and samples
// come from POST /api/issues/query — the same matcher behind the Issues table —
// so each card's number is exactly the row count its link opens.
export const HOME_ISSUE_CARDS: HomeIssueCard[] = [
  {
    key: "issues-fix-review",
    title: "Fixes to review",
    blurb: "The auto-fix factory proved a fix for each — read the change and the reviewer's verdict, then open the PR or send it back.",
    fix: "review",
    lead: true,
  },
  {
    key: "issues-fix-question",
    title: "Questions for you",
    blurb: "The fixing agents read these reports differently — pick what should happen and the fix continues.",
    fix: "question",
  },
  {
    key: "issues-came-back",
    title: "Authors came back",
    blurb: "We closed the author's issue or PR and they answered: reopened the issue, or filed this one linking what we closed. Take another look.",
    cameBack: true,
  },
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
  if (card.cameBack) return "/issues?came_back=1";
  if (card.fix) return `/issues?fix=${encodeURIComponent(card.fix)}`;
  return `/issues?disposition=${encodeURIComponent(card.disposition ?? "")}`;
}
