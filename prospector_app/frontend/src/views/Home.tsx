import { useEffect, useState } from "react";
import { Link } from "react-router";
import {
  api, type AdvisoryQueryResult, type AlertQueryResult, type IssueRow, type PRRow,
  type QueryResult,
} from "../api";
import { LinkedIssues } from "../components/LinkedIssues";
import { PRLink } from "../components/PRLink";
import { useIssueFlyout } from "../useIssueFlyout";
import { useJobStream } from "../useJobStream";
import { useRepoMeta } from "../RepoMetaContext";
import { useSystemHealth } from "../useSystemHealth";
import { SkeletonRows } from "../components/SkeletonRows";
import {
  breakdownHref, exploreHref, HOME_BREAKDOWN_ENTRIES, HOME_CARDS, HOME_COUNT_SPECS,
  HOME_ISSUE_CARDS, issuesHref, painLabel,
  SAMPLE_ISSUE_LIMIT, SAMPLE_LIMIT, SAMPLE_QUERY,
  SECURITY_ADVISORIES_HREF, SECURITY_ADVISORY_QUERY, SECURITY_ALERT_QUERY,
  SECURITY_ALERTS_HREF, SECURITY_CARD,
  type HomeCard, type HomeIssueAction, type HomeIssueCard, type HomeRowAction,
} from "./homeCards";

// While the backend snapshot is cold-loading, counts come back null with
// loading:true — re-ask on this cadence until real numbers arrive.
const COUNTS_POLL_MS = 1500;

// A sample row's job button: starts the card's per-PR job for this row and
// shows it running (reattaching to a run already going, started anywhere).
// `onDone` fires when the job finishes so the host can refresh its data.
function RowJobButton({ action, pr, onDone }: { action: HomeRowAction; pr: number; onDone: () => void }) {
  const { running, start } = useJobStream(action.kind, { pr }, () => onDone());
  return (
    <button className="link-btn home-row-run" disabled={running} title={action.hint}
      onClick={() => start(`/api/jobs/run/${action.kind}?pr=${pr}`)}>
      {running ? "running…" : `▶ ${action.label}`}
    </button>
  );
}

// One sample PR row in a Home card's table: number (opens the detail flyout),
// title, Community Pain Score, and the issues the PR fixes (open the issue
// flyout) — plus the card's per-PR job button when it carries one.
function SamplePR({ r, rowAction, onActionDone }: {
  r: PRRow;
  rowAction?: HomeRowAction;
  onActionDone: () => void;
}) {
  return (
    <tr className="home-sample-row">
      <td className="home-sample-pr">
        <PRLink n={r.number} className="mono" />
      </td>
      <td className="home-sample-title" title={r.summary?.one_liner ?? undefined}>
        {r.title ?? "(no title)"}
      </td>
      <td className="home-sample-pain mono small" title="Community Pain Score — linked-issue pain + PR engagement">
        {painLabel(r.pain_score)}
      </td>
      <td className="home-sample-issues small">
        <LinkedIssues issues={r.issues} limit={SAMPLE_ISSUE_LIMIT} />
      </td>
      {rowAction && (
        <td className="home-sample-run small">
          <RowJobButton action={rowAction} pr={r.number} onDone={onActionDone} />
        </td>
      )}
    </tr>
  );
}

// One Home card: the headline count + title link into the Explorer, and a
// small table of the card's highest-pain member PRs fills the rest — below the
// head inside a track column, to its right on the full-width backstop. `count`
// is null until the counts poll lands; `sample` is null until the sample query
// (started once the counts land) resolves.
function HomeCardRow({ card, count, sample, breakdown, onActionDone }: {
  card: HomeCard;
  count: number | null;
  sample: QueryResult | null;
  breakdown: { label: string; href: string; count: number | null }[];
  onActionDone: () => void;
}) {
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
      {breakdown.length > 0 && (
        <div className="home-breakdown small">
          {breakdown.filter((b) => b.count !== 0).map((b) => (
            <Link key={b.label} to={b.href} className="home-breakdown-item"
              title={`Open the ${b.label} PRs in the PR Explorer`}>
              <span className="mono">{b.count ?? "…"}</span> {b.label}
            </Link>
          ))}
        </div>
      )}
      <div className="home-card-side">
        {sample && sample.total > 0 && (
          <>
            <table className="home-sample-table">
              <tbody>
                {sample.items.map((r) => (
                  <SamplePR key={r.number} r={r} rowAction={card.rowAction} onActionDone={onActionDone} />
                ))}
              </tbody>
            </table>
            <Link to={href} className="home-show-all small">
              Show all {sample.total} in the Explorer →
            </Link>
          </>
        )}
        {total === 0 && <div className="muted small">None right now.</div>}
      </div>
    </div>
  );
}

const SECURITY_SEVERITY_CLS: Record<string, string> = {
  critical: "chip-red", high: "chip-red", medium: "chip-yellow",
};
const SECURITY_SEVERITY_RANK: Record<string, number> = {
  critical: 3, high: 2, medium: 1, low: 0,
};

// How many merged sample rows the Security card shows: one more than the PR
// cards' SAMPLE_LIMIT, so an open secret alert still surfaces when advisories
// fill their own slice.
const SECURITY_SAMPLE_LIMIT = SAMPLE_LIMIT + 1;

// One Security sample row, whichever store it came from: the link opens its
// detail panel in the Security views.
interface SecuritySample {
  key: string;
  severity: string;
  created: string;
  label: string;
  href: string;
}

// The Security card under "Your move": open critical/high advisories the
// find-fixed pass still marks not-fixed, plus every open secret-scanning
// alert. Each side is counted by the same query its link opens; the sample
// merges both most severe first, ties oldest first, so an open critical item
// leads it.
function HomeSecurityCard() {
  const [data, setData] = useState<{ advisories: AdvisoryQueryResult; alerts: AlertQueryResult } | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let cancelled = false;
    Promise.all([api.queryAdvisories(SECURITY_ADVISORY_QUERY), api.queryAlerts(SECURITY_ALERT_QUERY)])
      .then(([advisories, alerts]) => { if (!cancelled) { setData({ advisories, alerts }); setFailed(false); } })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, []);
  const total = data ? data.advisories.total + data.alerts.total : null;
  const samples: SecuritySample[] = data
    ? [
      ...data.advisories.items.map((r) => ({
        key: r.ghsa_id,
        severity: r.severity,
        created: r.created_at ?? "",
        label: r.summary ?? r.ghsa_id,
        href: `${SECURITY_ADVISORIES_HREF}&advisory=${encodeURIComponent(r.ghsa_id)}`,
      })),
      ...data.alerts.items.map((r) => ({
        key: `${r.source}/${r.number}`,
        severity: r.severity,
        created: r.created_at ?? "",
        label: r.title ?? r.secret_type ?? `#${r.number}`,
        href: `${SECURITY_ALERTS_HREF}&alert_source=${r.source}&alert=${r.number}`,
      })),
    ]
      .sort((a, b) =>
        (SECURITY_SEVERITY_RANK[b.severity] ?? -1) - (SECURITY_SEVERITY_RANK[a.severity] ?? -1)
        || a.created.localeCompare(b.created))
      .slice(0, SECURITY_SAMPLE_LIMIT)
    : [];
  const breakdown = data
    ? [
      { label: "advisories not fixed", count: data.advisories.total, href: SECURITY_ADVISORIES_HREF },
      { label: "open secret alerts", count: data.alerts.total, href: SECURITY_ALERTS_HREF },
    ].filter((b) => b.count !== 0)
    : [];
  return (
    <div className="act-card home-card">
      <Link to={SECURITY_CARD.href} className="home-card-head act-card-clickable" title="Open the Security views">
        <div className={"act-card-n" + (total === null && !failed ? " home-count-loading" : "")}>
          {failed ? "?" : total ?? "…"}
        </div>
        <div className="home-card-text">
          <div className="act-card-l">{SECURITY_CARD.title}</div>
          <div className="small muted home-card-blurb">{SECURITY_CARD.blurb}</div>
        </div>
      </Link>
      {breakdown.length > 0 && (
        <div className="home-breakdown small">
          {breakdown.map((b) => (
            <Link key={b.label} to={b.href} className="home-breakdown-item"
              title={`Open the ${b.label} in the Security views`}>
              <span className="mono">{b.count}</span> {b.label}
            </Link>
          ))}
        </div>
      )}
      <div className="home-card-side">
        {failed && <div className="muted small">Failed to load security data.</div>}
        {samples.length > 0 && (
          <>
            <table className="home-sample-table">
              <tbody>
                {samples.map((s) => (
                  <tr key={s.key} className="home-sample-row">
                    <td className="home-sample-title" title={s.label}>
                      <Link to={s.href}>{s.label}</Link>
                    </td>
                    <td className="home-sample-pain small">
                      <span className={`chip ${SECURITY_SEVERITY_CLS[s.severity] ?? "chip-muted"} sm`}
                        title="Severity">{s.severity}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {total !== null && total > samples.length && (
              <Link to={SECURITY_CARD.href} className="home-show-all small">
                Show all {total} in Security →
              </Link>
            )}
          </>
        )}
        {total === 0 && <div className="muted small">None right now.</div>}
      </div>
    </div>
  );
}

// One sample issue row: number (opens the issue flyout; ⌘-click for GitHub),
// title, and pain rank.
function SampleIssue({ r }: { r: IssueRow }) {
  const { openIssue } = useIssueFlyout();
  const { issueUrl } = useRepoMeta();
  return (
    <tr className="home-sample-row">
      <td className="home-sample-pr">
        <a href={issueUrl(r.number)} target="_blank" rel="noreferrer" className="mono"
          title="Open in this panel (⌘-click for GitHub ↗)"
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey) return;
            e.preventDefault();
            openIssue(r.number);
          }}>#{r.number}</a>
      </td>
      <td className="home-sample-title">{r.title ?? "(no title)"}</td>
      <td className="home-sample-pain mono small" title="Pain rank — higher = more community impact">
        {painLabel(r.pain)}
      </td>
    </tr>
  );
}

// An issue card's job button — the same run the Control tab's button makes,
// streamed here with its latest output line while it goes. The batch is the
// card's cap bounded by how many issues actually need the job.
function IssueCardAction({ action, total, onDone }: {
  action: HomeIssueAction;
  total: number;
  onDone: () => void;
}) {
  const { log, running, start } = useJobStream(action.kind, {}, () => onDone());
  const tail = log.length > 0 ? log[log.length - 1] : null;
  return (
    <div className="home-card-action">
      <button className="btn-secondary sm" disabled={running}
        title="Run this job now — same run as the Control tab's button, streamed here."
        onClick={() => start(`/api/jobs/run/${action.kind}?count=${Math.min(action.batch, total)}`)}>
        {running ? "Running…" : `▶ ${action.label}`}
      </button>
      {running && tail && <span className="muted small home-action-tail">{tail}</span>}
    </div>
  );
}

// One Home issue card: the headline count + title link into the Issues view
// filtered to the card's disposition, a small table of its highest-pain
// issues, and — when the card carries an action — the job button that moves
// them forward. Count and sample come from one issues query the card runs
// itself, re-fetched when its job finishes.
function HomeIssueCardRow({ card }: { card: HomeIssueCard }) {
  const [sample, setSample] = useState<{ items: IssueRow[]; total: number } | null>(null);
  const [failed, setFailed] = useState(false);
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    let cancelled = false;
    api.queryIssues({
      disposition: card.disposition, state: "open",
      sort: "pain", direction: "desc", limit: SAMPLE_LIMIT,
    })
      .then((d) => { if (!cancelled) { setSample({ items: d.items, total: d.total }); setFailed(false); } })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, [card.disposition, generation]);
  const href = issuesHref(card);
  const total = sample ? sample.total : null;
  return (
    <div className={"act-card home-card" + (card.lead ? " act-card-lead" : "")}>
      <Link to={href} className="home-card-head act-card-clickable" title="Open these issues in the Issues view">
        <div className={"act-card-n" + (total === null && !failed ? " home-count-loading" : "")}>
          {failed ? "?" : total ?? "…"}
        </div>
        <div className="home-card-text">
          <div className="act-card-l">{card.title}</div>
          <div className="small muted home-card-blurb">{card.blurb}</div>
        </div>
      </Link>
      <div className="home-card-side">
        {failed && <div className="muted small">Failed to load issues.</div>}
        {sample && sample.total > 0 && (
          <>
            <table className="home-sample-table">
              <tbody>
                {sample.items.map((r) => <SampleIssue key={r.number} r={r} />)}
              </tbody>
            </table>
            <Link to={href} className="home-show-all small">
              Show all {sample.total} in Issues →
            </Link>
          </>
        )}
        {total === 0 && <div className="muted small">None right now.</div>}
      </div>
      {card.action && total !== null && total > 0 && (
        <IssueCardAction action={card.action} total={total}
          onDone={() => setGeneration((g) => g + 1)} />
      )}
    </div>
  );
}

export default function Home() {
  // While every known worker lane is down (tripped or its machine offline),
  // the workers' column is not actually moving: retitle it and grey its cards.
  const stalled = useSystemHealth()?.workers_stalled ?? false;
  const [counts, setCounts] = useState<number[] | null>(null);
  const [samples, setSamples] = useState<QueryResult[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Bumped when a sample row's job finishes, so the PR counts + samples
  // refetch and a PR the job moved forward changes cards.
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      api.prCounts(HOME_COUNT_SPECS)
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
  }, [generation]);
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
  // Counts and samples are index-aligned with the flat HOME_CARDS array, so
  // each column looks a card's data up by its position there.
  const renderCard = (card: HomeCard) => {
    const i = HOME_CARDS.indexOf(card);
    const breakdown = HOME_BREAKDOWN_ENTRIES
      .map((e, j) => ({ ...e, j }))
      .filter((e) => e.cardKey === card.key)
      .map((e) => ({
        label: e.entry.label,
        href: breakdownHref(card, e.entry),
        count: counts ? counts[HOME_CARDS.length + e.j] : null,
      }));
    return (
      <HomeCardRow key={card.key} card={card} count={counts ? counts[i] : null}
        sample={samples ? samples[i] : null} breakdown={breakdown}
        onActionDone={() => setGeneration((g) => g + 1)} />
    );
  };
  return (
    <div className="home">
      <div className="home-head">
        <h2>Home</h2>
        <div className="muted small">
          Every open PR, by whose move it is: yours, the workers&apos;, or a person&apos;s after the
          automation handed it back. Each card samples its highest-pain members and opens the matching view.
        </div>
      </div>
      {err && <div className="error">Failed to load PRs: {err}</div>}
      {loading && <SkeletonRows label="Loading PR data…" />}
      <div className="home-columns">
        <section className="home-col">
          <div className="home-col-head muted">Your move — one click each</div>
          {HOME_CARDS.filter((c) => c.column === "act").map(renderCard)}
          <HomeSecurityCard />
        </section>
        <section className={"home-col" + (stalled ? " home-col-stalled" : "")}>
          <div className="home-col-head muted">
            {stalled ? "Stalled — the workers are down" : "In motion — the workers clear these"}
          </div>
          {HOME_CARDS.filter((c) => c.column === "auto").map(renderCard)}
        </section>
        <section className="home-col">
          <div className="home-col-head muted">Handed back — needs a person</div>
          {HOME_CARDS.filter((c) => c.column === "handed").map(renderCard)}
        </section>
        <section className="home-col">
          <div className="home-col-head muted">Issues — triage picks to action</div>
          {HOME_ISSUE_CARDS.map((c) => <HomeIssueCardRow key={c.key} card={c} />)}
        </section>
      </div>
    </div>
  );
}
