import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api, type PRRow, type QueryResult } from "../api";
import { LinkedIssues } from "../components/LinkedIssues";
import { PRLink } from "../components/PRLink";
import { exploreHref, HOME_CARDS, SAMPLE_QUERY, type HomeCard } from "./homeCards";

// One sample PR inside a Home card: number (opens the detail flyout), title,
// Community Pain Score, and the issues the PR fixes (open the issue flyout).
function SamplePR({ r }: { r: PRRow }) {
  const pain = r.pain_score;
  return (
    <div className="home-sample-row">
      <PRLink n={r.number} className="home-sample-pr mono" />
      <span className="home-sample-title" title={r.summary?.one_liner ?? undefined}>
        {r.title ?? "(no title)"}
      </span>
      <span className="home-sample-pain mono small" title="Community Pain Score — linked-issue pain + PR engagement">
        {pain ? `🔥 ${pain.toFixed(2)}` : ""}
      </span>
      <span className="home-sample-issues small">
        <LinkedIssues issues={r.issues} limit={3} />
      </span>
    </div>
  );
}

// One Home card row: the headline count + title link into the Explorer, and
// the card's highest-pain member PRs sampled inline underneath.
function HomeCardRow({ card, sample }: { card: HomeCard; sample: QueryResult | null }) {
  const href = exploreHref(card);
  return (
    <div className={"act-card home-card" + (card.lead ? " act-card-lead" : "")}>
      <Link to={href} className="home-card-head act-card-clickable" title="Open these PRs in the PR Explorer">
        <div className="act-card-n">{sample ? sample.total : "…"}</div>
        <div className="home-card-text">
          <div className="act-card-l">{card.title}</div>
          <div className="small muted home-card-blurb">{card.blurb}</div>
        </div>
      </Link>
      {sample && sample.total > 0 && (
        <div className="home-card-sample">
          {sample.items.map((r) => <SamplePR key={r.number} r={r} />)}
          <Link to={href} className="home-show-all small">
            Show all {sample.total} in the Explorer →
          </Link>
        </div>
      )}
      {sample && sample.total === 0 && (
        <div className="home-card-sample muted small">None right now.</div>
      )}
    </div>
  );
}

export default function Home() {
  const [samples, setSamples] = useState<QueryResult[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    Promise.all(HOME_CARDS.map((c) => api.queryPrs(c.spec, SAMPLE_QUERY)))
      .then(setSamples)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)));
  }, []);
  return (
    <div className="home">
      <div className="home-head">
        <h2>Home</h2>
        <div className="muted small">
          The most actionable PRs right now — each card samples its highest-pain PRs and opens the
          PR Explorer with the matching filters.
        </div>
      </div>
      {err && <div className="error">Failed to load PRs: {err}</div>}
      <div className="home-cards">
        {HOME_CARDS.map((card, i) => (
          <HomeCardRow key={card.key} card={card} sample={samples ? samples[i] : null} />
        ))}
      </div>
    </div>
  );
}
