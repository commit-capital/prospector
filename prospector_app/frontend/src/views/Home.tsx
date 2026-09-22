import { useEffect, useState } from "react";
import { Link } from "react-router";
import {
  api, type AdvisoryQueryResult, type AdvisorySeverity, type AlertQueryResult,
  type IssueRow, type PRRow, type QueryResult,
} from "../api";
import { LinkedIssues } from "../components/LinkedIssues";
import { PRLink } from "../components/PRLink";
import { useIssueFlyout } from "../useIssueFlyout";
import { useJobStream } from "../useJobStream";
import { useRepoMeta } from "../RepoMetaContext";
import {
  breakdownHref, exploreHref, HOME_BREAKDOWN_ENTRIES, HOME_CARDS, HOME_COUNT_SPECS,
  HOME_ISSUE_CARDS, HOME_SECURITY_CARD, issuesHref, painLabel,
  SAMPLE_ISSUE_LIMIT, SAMPLE_LIMIT, SAMPLE_QUERY,
  SECURITY_ADVISORY_QUERY, SECURITY_SECRET_QUERY, securityHref,
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

// The Security card: critical/high advisories the find-fixed pass has not
// cleared, plus any open leaked secret, counted from the same queries the
// Alerts tab serves. Samples list the worst items — secrets first, then
// advisories — and every link opens the matching Alerts sub-view.
function HomeSecurityCardRow() {
  const [res, setRes] = useState<{ advisories: AdvisoryQueryResult; secrets: AlertQueryResult } | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let cancelled = false;
    Promise.all([
      api.queryAdvisories(SECURITY_ADVISORY_QUERY),
      api.queryAlerts(SECURITY_SECRET_QUERY),
    ])
      .then(([advisories, secrets]) => { if (!cancelled) { setRes({ advisories, secrets }); setFailed(false); } })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, []);
  const total = res ? res.advisories.total + res.secrets.total : null;
  const headHref = res && res.secrets.total > 0 && res.advisories.total === 0
    ? securityHref("alerts")
    : securityHref("advisories");
  const samples: { key: string; severity: AdvisorySeverity; title: string; href: string }[] = res
    ? [
        ...res.secrets.items.map((r) => ({
          key: `alert-${r.id}`, severity: r.severity,
          title: r.title ?? r.secret_type ?? "(secret)", href: securityHref("alerts"),
        })),
        ...res.advisories.items.map((r) => ({
          key: `advisory-${r.id}`, severity: r.severity,
          title: r.summary ?? r.ghsa_id, href: securityHref("advisories"),
        })),
      ].slice(0, SAMPLE_LIMIT)
    : [];
  return (
    <div className="act-card home-card">
      <Link to={headHref} className="home-card-head act-card-clickable" title="Open these in the Alerts tab">
        <div className={"act-card-n" + (total === null && !failed ? " home-count-loading" : "")}>
          {failed ? "?" : total ?? "…"}
        </div>
        <div className="home-card-text">
          <div className="act-card-l">{HOME_SECURITY_CARD.title}</div>
          <div className="small muted home-card-blurb">{HOME_SECURITY_CARD.blurb}</div>
        </div>
      </Link>
      {res !== null && total !== null && total > 0 && (
        <div className="home-breakdown small">
          {res.advisories.total > 0 && (
            <Link to={securityHref("advisories")} className="home-breakdown-item"
              title="Open the Advisories view">
              <span className="mono">{res.advisories.total}</span> advisories
            </Link>
          )}
          {res.secrets.total > 0 && (
            <Link to={securityHref("alerts")} className="home-breakdown-item"
              title="Open the Alerts view — open secrets sort first">
              <span className="mono">{res.secrets.total}</span> leaked secrets
            </Link>
          )}
        </div>
      )}
      <div className="home-card-side">
        {failed && <div className="muted small">Failed to load security data.</div>}
        {samples.length > 0 && (
          <table className="home-sample-table">
            <tbody>
              {samples.map((s) => (
                <tr key={s.key} className="home-sample-row">
                  <td className="home-sample-pr mono small">{s.severity}</td>
                  <td className="home-sample-title"><Link to={s.href}>{s.title}</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {total === 0 && <div className="muted small">None right now.</div>}
      </div>
    </div>
  );
}

export default function Home() {
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
      {loading && (
        <div className="home-loading" role="status">
          <span className="spinner" /> Loading PR data…
        </div>
      )}
      <div className="home-columns">
        <section className="home-col">
          <div className="home-col-head muted">Your move — one click each</div>
          {HOME_CARDS.filter((c) => c.column === "act").map(renderCard)}
          <HomeSecurityCardRow />
        </section>
        <section className="home-col">
          <div className="home-col-head muted">In motion — the workers clear these</div>
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
