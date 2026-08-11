import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api, type CheckClause, type FilterSpec } from "../api";
import { CHECK_DEFS } from "../components/explorer/checkDefs";

// One Home card: a headline count over a filter spec, linking to the PR
// Explorer with that spec (plus an optional sort) in the URL.
interface HomeCard {
  key: string;
  title: string;
  blurb: string;
  spec: FilterSpec;
  sort?: string;
  dir?: "asc" | "desc";
  lead?: boolean;
}

function checksPass(...keys: string[]): CheckClause[] {
  return keys.map((key) => ({ key, status: "pass" }));
}

// Every check key the checks rollup carries, required to pass.
const ALL_CHECKS_PASS: CheckClause[] = checksPass(...CHECK_DEFS.map((d) => d.key));

// The cards, most actionable first. Counts come from the backend matcher
// (POST /api/prs/counts), so each card's number is exactly the row count the
// Explorer shows when its link opens.
const HOME_CARDS: HomeCard[] = [
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

function exploreHref(card: HomeCard): string {
  const params = new URLSearchParams();
  params.set("spec", JSON.stringify(card.spec));
  if (card.sort) params.set("sort", card.sort);
  if (card.dir) params.set("dir", card.dir);
  return `/explore?${params}`;
}

export default function Home() {
  const [counts, setCounts] = useState<number[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.prCounts(HOME_CARDS.map((c) => c.spec))
      .then((r) => setCounts(r.counts))
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
  }, []);
  return (
    <div className="home">
      <div className="home-head">
        <h2>Home</h2>
        <div className="muted small">
          The most actionable PRs right now — each card opens the PR Explorer with the matching filters.
        </div>
      </div>
      {err && <div className="error">Failed to load counts: {err}</div>}
      <div className="act-cards home-cards">
        {HOME_CARDS.map((card, i) => (
          <Link
            key={card.key}
            to={exploreHref(card)}
            className={"act-card act-card-clickable home-card" + (card.lead ? " act-card-lead" : "")}
            title="Open these PRs in the PR Explorer"
          >
            <div className="act-card-n">{counts ? counts[i] : "…"}</div>
            <div className="act-card-l">{card.title}</div>
            <div className="small muted home-card-blurb">{card.blurb}</div>
          </Link>
        ))}
      </div>
    </div>
  );
}
