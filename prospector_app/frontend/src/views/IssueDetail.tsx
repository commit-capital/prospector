import { useEffect, useState } from "react";
import { Link } from "react-router";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type IssueDetail } from "../api";
import { useIssueFlyout } from "../useIssueFlyout";
import { useRepoMeta } from "../RepoMetaContext";
import { TrustedAuthorName } from "../components/TrustedAuthor";
import { DispositionChip, LinkedPRs, ReproChip } from "./Issues";
import { IssueActionBar } from "../components/IssueActionBar";

// An issue reference inside the flyout: a plain click swaps this panel to that
// issue; a modifier-click follows the href to github.com.
function FlyoutIssueLink({ n }: { n: number }) {
  const { openIssue } = useIssueFlyout();
  const { issueUrl } = useRepoMeta();
  return (
    <a href={issueUrl(n)} target="_blank" rel="noreferrer"
       className="gh-pr-link" title="Open in this panel (⌘-click for GitHub ↗)"
       onClick={(e) => {
         if (e.metaKey || e.ctrlKey || e.shiftKey) return;
         e.preventDefault(); openIssue(n);
       }}>#{n}</a>
  );
}

/** One issue's full detail — meta, triage verdict (disposition + rationale +
 *  asks), dup-cluster context, linked PRs, the agent's plain-language summary
 *  (gist), and the report body. Rendered inside the issue flyout. */
export function IssueDetailContent({ issue }: { issue: number }) {
  const [d, setD] = useState<IssueDetail>();
  const [err, setErr] = useState<string>();
  const { prUrl } = useRepoMeta();

  useEffect(() => {
    api.getIssue(issue).then(setD).catch((e) => setErr(String(e)));
  }, [issue]);

  if (err) return <div className="error">Failed to load issue #{issue}: {err}</div>;
  if (!d) return <div className="muted" style={{ padding: 16 }}>Loading issue #{issue}…</div>;

  const a = d.analysis;
  return (
    <div className="prcontent">
      <div className="prc-head">
        <h2>#{d.number} — {d.title}</h2>
        <div className="metaline">
          {d.author && <a href={`https://github.com/${d.author}`} target="_blank" rel="noreferrer"
                          title="The reporter's GitHub profile">@<TrustedAuthorName author={d.author} trusted={d.trusted_author} /></a>}
          <span className={`chip sm ${d.state === "open" ? "chip-green" : "chip-muted"}`}>{d.state}</span>
          <DispositionChip d={d.disposition} />
          <ReproChip grade={d.repro_grade} />
          {d.pain != null && <span className="chip chip-purple sm" title="Cluster pain rank (higher = more impactful)">pain {d.pain.toFixed(2)}</span>}
          {d.subsystem && <span className="chip chip-muted sm">{d.subsystem}</span>}
          {d.cluster != null && (
            <Link className="chip chip-blue" to={`/issues?dup=${d.cluster}`}
                  title="This issue's dedup cluster — opens the Close-out tab (the card appears once the cluster is curated as duplicates)">
              cluster {d.cluster}</Link>
          )}
          <a className="chip chip-muted" href={d.url} target="_blank" rel="noreferrer">GitHub ↗</a>
        </div>
        <div className="muted small">
          opened {d.created_at?.slice(0, 10)} · updated {d.updated_at?.slice(0, 10)}
          {" "}· {d.comments} comment{d.comments === 1 ? "" : "s"} · {d.thumbs_up} 👍
        </div>
      </div>

      <IssueActionBar d={d} onActed={() => api.getIssue(issue).then(setD).catch(() => {})} />

      <h3>Triage</h3>
      {a ? (
        <div className="callout">
          <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 6 }}>
            <DispositionChip d={a.disposition} />
            {a.canonical != null && <span className="muted small">duplicate of <FlyoutIssueLink n={a.canonical} /></span>}
            {a.fixed_by != null && (
              <span className="muted small">fixed by <a href={prUrl(a.fixed_by)} target="_blank" rel="noreferrer"
                className="gh-pr-link" title="The merged PR the fix scan cites">#{a.fixed_by}</a></span>
            )}
          </div>
          {a.rationale && <div style={{ whiteSpace: "pre-wrap" }}>{a.rationale}</div>}
          {a.asks && a.asks.length > 0 && (
            <>
              <div className="muted small" style={{ marginTop: 8 }}>Asks for the reporter:</div>
              <ul style={{ margin: "4px 0 0 18px" }}>
                {a.asks.map((ask, i) => <li key={i}>{ask}</li>)}
              </ul>
            </>
          )}
        </div>
      ) : (
        <div className="muted small">Not yet analyzed — run Issue analyze from the Control tab.</div>
      )}

      {(d.cluster != null || d.duplicates.length > 0) && (
        <>
          <h3>Cluster</h3>
          <div className="muted small">
            {d.cluster_label && <b>{d.cluster_label} · </b>}
            cluster {d.cluster ?? "—"} · {d.cluster_size} member{d.cluster_size === 1 ? "" : "s"}
            {d.canonical != null && <> · canonical <FlyoutIssueLink n={d.canonical} /></>}
          </div>
          {d.duplicates.length > 0 && (
            <div className="muted small" style={{ marginTop: 4 }}>
              siblings: {d.duplicates.slice(0, 12).map((m, i) => <span key={m}>{i > 0 && " "}<FlyoutIssueLink n={m} /></span>)}
              {d.duplicates.length > 12 && ` +${d.duplicates.length - 12}`}
            </div>
          )}
        </>
      )}

      <h3>PRs that may fix it</h3>
      {d.linked_prs.length
        ? <LinkedPRs prs={d.linked_prs} count={d.linked_pr_count} referencedCount={d.referenced_pr_count} />
        : <span className="muted">—</span>}

      {a?.gist && (
        <>
          <h3>Summary</h3>
          <div className="callout" style={{ whiteSpace: "pre-wrap" }}>{a.gist}</div>
        </>
      )}

      {d.body && (
        <>
          <h3>Report</h3>
          <div className="markdown prbody-md">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{d.body.slice(0, 4000)}</ReactMarkdown>
          </div>
          {d.body.length > 4000 && <div className="muted small">… truncated — full text on GitHub ↗</div>}
        </>
      )}
    </div>
  );
}
