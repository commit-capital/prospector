import { useEffect, useState } from "react";
import { api, type PRHistoryItem } from "../api";
import { Collapsible } from "./Collapsible";
import { timeAgo } from "../timeAgo";

const KIND_ICON: Record<string, string> = {
  comment: "💬", review: "📝", greptile_review: "🔍", commit: "🔨",
  reopened: "♻️", closed: "🚫", force_push: "⏫", renamed: "✏️",
};

const KIND_VERB: Record<string, string> = {
  comment: "commented", review: "reviewed", greptile_review: "reviewed",
  commit: "pushed", reopened: "reopened this PR", closed: "closed this PR",
  force_push: "force-pushed the branch", renamed: "renamed this PR",
};

/** Condensed upstream activity for one PR — comments, reviews (Greptile's
 *  score history flagged), commits, and reopen/close/force-push/rename
 *  events, oldest first — so a reviewer can follow the back-and-forth
 *  without leaving the cockpit. Hidden while loading finds nothing. */
export function PRHistory({ pr }: { pr: number }) {
  const [items, setItems] = useState<PRHistoryItem[] | null>(null);

  useEffect(() => {
    let live = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset before fetching the newly selected PR
    setItems(null);
    api.prHistory(pr).then((r) => { if (live) setItems(r.items); }).catch(() => { if (live) setItems([]); });
    return () => { live = false; };
  }, [pr]);

  if (!items || items.length === 0) return null;

  return (
    <section className="prc-section">
      <Collapsible summary={<>🕘 History <span className="count">{items.length}</span></>}>
        <ul className="pr-history-list">
          {items.map((it, i) => {
            const body = it.kind === "greptile_review" && it.greptile_score != null
              ? `confidence ${it.greptile_score}/5`
              : it.summary;
            return (
              <li key={`${it.at}-${i}`} className="ph-row">
                <span className="ph-icon" aria-hidden="true">{KIND_ICON[it.kind] ?? "•"}</span>
                <span className="ph-body">
                  {it.actor && <b>{it.actor}</b>} {KIND_VERB[it.kind] ?? it.kind}
                  {it.state && it.kind === "review" && <span className="muted"> ({it.state})</span>}
                  {body && <span className="ph-text"> — {body}</span>}
                  {it.url && (
                    <a className="action-log-link" href={it.url} target="_blank" rel="noreferrer" title="view on GitHub ↗"> ↗</a>
                  )}
                </span>
                <span className="muted small ph-time" title={it.at}>{timeAgo(it.at)}</span>
              </li>
            );
          })}
        </ul>
      </Collapsible>
    </section>
  );
}
