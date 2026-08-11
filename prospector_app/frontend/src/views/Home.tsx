import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api, type PRRow, type QueryResult } from "../api";
import { LinkedIssues } from "../components/LinkedIssues";
import { PRLink } from "../components/PRLink";
import { exploreHref, HOME_CARDS, SAMPLE_QUERY, type HomeCard } from "./homeCards";

// While the backend snapshot is cold-loading, counts come back null with
// loading:true — re-ask on this cadence until real numbers arrive.
const COUNTS_POLL_MS = 1500;

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
// the card's highest-pain member PRs sampled inline underneath. `count` is
// null until the counts poll lands; `sample` is null until the sample query
// (started once the counts land) resolves.
function HomeCardRow({ card, count, sample }: { card: HomeCard; count: number | null; sample: QueryResult | null }) {
  const href = exploreHref(card);
  const total = sample ? sample.total : count;
  return (
    <div className={"act-card home-card" + (card.lead ? " act-card-lead" : "")}>
      <Link to={href} className="home-card-head act-card-clickable" title="Open these PRs in the PR Explorer">
        <div className={"act-card-n" + (total === null ? " home-count-loading" : "")}>
          {total ?? "…"}
        </div>
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
      {total === 0 && <div className="home-card-sample muted small">None right now.</div>}
    </div>
  );
}

export default function Home() {
  const [counts, setCounts] = useState<number[] | null>(null);
  const [samples, setSamples] = useState<QueryResult[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      api.prCounts(HOME_CARDS.map((c) => c.spec))
        .then((r) => {
          if (cancelled) return;
          if (r.counts) setCounts(r.counts);
          else timer = window.setTimeout(load, COUNTS_POLL_MS);
        })
        .catch((e: unknown) => {
          if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
        });
    };
    load();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, []);
  // Each card's inline sample. Fetched only once the counts poll has landed —
  // the snapshot is published by then, so these queries serve from memory.
  useEffect(() => {
    if (counts === null) return;
    let cancelled = false;
    Promise.all(HOME_CARDS.map((c) => api.queryPrs(c.spec, SAMPLE_QUERY)))
      .then((r) => { if (!cancelled) setSamples(r); })
      .catch((e: unknown) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => { cancelled = true; };
  }, [counts]);
  const loading = counts === null && !err;
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
      {loading && (
        <div className="home-loading" role="status">
          <span className="spinner" /> Loading PR data…
        </div>
      )}
      <div className="home-cards">
        {HOME_CARDS.map((card, i) => (
          <HomeCardRow key={card.key} card={card} count={counts ? counts[i] : null}
            sample={samples ? samples[i] : null} />
        ))}
      </div>
    </div>
  );
}
