import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import { api, type IssueDetail } from "../api";
import { TrustedAuthorName } from "../components/TrustedAuthor";
import { IssueSummary } from "../components/issues/IssueSummary";
import { FixStatusChip, IssueFixPanel } from "../components/IssueFixPanel";
import { FlyoutIssueLink } from "../components/FlyoutIssueLink";

/** One issue's full detail: the Summary panel, the auto-fix lane, and the
 *  dup-cluster context. Rendered inside the issue flyout. */
export function IssueDetailContent({ issue }: { issue: number }) {
  const [d, setD] = useState<IssueDetail>();
  const [err, setErr] = useState<string>();

  useEffect(() => {
    api.getIssue(issue).then(setD).catch((e) => setErr(String(e)));
  }, [issue]);
  const reload = useCallback(() => { api.getIssue(issue).then(setD).catch(() => {}); }, [issue]);

  if (err) return <div className="error">Failed to load issue #{issue}: {err}</div>;
  if (!d) return <div className="muted" style={{ padding: 16 }}>Loading issue #{issue}…</div>;

  return (
    <div className="prcontent">
      <div className="prc-head">
        <h2>#{d.number} — {d.title}</h2>
        <div className="metaline">
          {d.author && <a href={`https://github.com/${d.author}`} target="_blank" rel="noreferrer"
                          title="The reporter's GitHub profile">@<TrustedAuthorName author={d.author} trusted={d.trusted_author} /></a>}
          <span className={`chip sm ${d.state === "open" ? "chip-green" : "chip-muted"}`}>{d.state}</span>
          <FixStatusChip status={d.fix_status} reason={d.fix_reason} />
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

      <IssueSummary d={d} onChanged={reload} />

      <section className="panel">
        <div className="panel-head"><h3>Other actions</h3></div>
        <h4>Auto-fix</h4>
        <IssueFixPanel d={d} onChanged={reload} />
      </section>

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
    </div>
  );
}
