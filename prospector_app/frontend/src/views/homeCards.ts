import type { CheckClause, FilterSpec } from "../api";
// Explicit .ts extension so the node:test runner (type stripping, no bundler)
// can resolve this runtime import when homeCards.test.ts loads the module.
import { CHECK_DEFS } from "../components/explorer/checkDefs.ts";

// One Home card: a headline count over a filter spec, linking to the PR
// Explorer with that spec (plus an optional sort) in the URL.
export interface HomeCard {
  key: string;
  title: string;
  blurb: string;
  spec: FilterSpec;
  sort?: string;
  dir?: "asc" | "desc";
  lead?: boolean;
}

// How many sample PRs each card fetches and shows inline; the "Show all"
// link opens the full set in the Explorer.
export const SAMPLE_LIMIT = 4;

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

// Every check key the checks rollup carries, required to pass.
export const ALL_CHECKS_PASS: CheckClause[] = checksPass(...CHECK_DEFS.map((d) => d.key));

// The cards, most actionable first. Counts and samples come from the backend
// matcher (POST /api/prs/query), so each card's number is exactly the row
// count the Explorer shows when its link opens.
export const HOME_CARDS: HomeCard[] = [
  {
    key: "ready",
    title: "PRs ready to merge",
    blurb: "Every check green — review at the bar, CI passing, security GREEN, verified. Just merge.",
    spec: {
      checks: ALL_CHECKS_PASS,
      greptile: { op: ">", value: 4 },
      greptile_stale: false,
      safety: "GREEN",
    },
    sort: "updated",
    dir: "asc",
    lead: true,
  },
  {
    key: "base-update",
    title: "Just need a base update",
    blurb: "Green everywhere except merge conflicts — update the branch with the base and they can go fully green.",
    spec: {
      checks: [
        ...checksPass("review", "ci", "tests", "secrets", "security", "verify"),
        { key: "mergeable", status: "fail" },
      ],
      greptile: { op: ">", value: 4 },
      greptile_stale: false,
      safety: "GREEN",
    },
  },
  {
    key: "nitpicks",
    title: "Just need nitpicks fixed",
    blurb: "CI passing, no conflicts, but the review left nits below the bar — a nitpick pass unblocks them.",
    spec: {
      checks: checksPass("ci", "mergeable"),
      greptile_severity: "nits",
    },
  },
  {
    key: "security-pending",
    title: "Awaiting security review",
    blurb: "Clean on review, CI, conflicts, and secrets, but the deep security review has never run.",
    spec: {
      checks: [
        ...checksPass("review", "ci", "mergeable", "secrets"),
        { key: "security", status: "never_ran" },
      ],
    },
  },
  {
    key: "verify-pending",
    title: "Awaiting verification",
    blurb: "Security GREEN and otherwise clean, but dynamic verification has never run.",
    spec: {
      checks: [
        ...checksPass("review", "ci", "mergeable", "secrets", "security"),
        { key: "verify", status: "never_ran" },
      ],
      safety: "GREEN",
    },
  },
  {
    key: "needs-human",
    title: "Need a human decision",
    blurb: "Flagged needs-human or safety RED — only an operator call moves these forward.",
    spec: { preset: "needs-human" },
  },
];

export function exploreHref(card: HomeCard): string {
  const params = new URLSearchParams();
  params.set("spec", JSON.stringify(card.spec));
  if (card.sort) params.set("sort", card.sort);
  if (card.dir) params.set("dir", card.dir);
  return `/explore?${params}`;
}
