import { useEffect, useState } from "react";
import { api, type PRAction } from "../api";
import { useExec } from "../ExecContext";
import { timeAgo } from "../timeAgo";

// semantic kind → human verb. The bot identity does it; the operator authorizes.
const VERB: Record<string, string> = {
  close: "closed", merge: "merged", reopen: "reopened",
  review: "requested changes on", comment: "commented on",
};

/** What the configured bot has actually done to this PR, and who initiated each.
 *  Hidden when no
 *  real action has landed yet. `refresh` bumps to re-fetch after a new action. */
export function PRActionLog({ pr, refresh = 0 }: { pr: number; refresh?: number }) {
  const { botLogin } = useExec();
  const [items, setItems] = useState<PRAction[] | null>(null);
  useEffect(() => {
    let live = true;
    api.prActions(pr).then((r) => { if (live) setItems(r.items); }).catch(() => {});
    return () => { live = false; };
  }, [pr, refresh]);

  if (!items || items.length === 0) return null;
  return (
    <div className="action-log">
      <div className="action-log-title">Actions on this PR</div>
      <ul>
        {items.map((a, i) => {
          const verb = VERB[a.kind] ?? a.kind;
          // deep-link the verb to the exact GitHub event when we captured one;
          // otherwise plain text — no redundant link to the PR itself.
          const verbEl = a.event_url
            ? <a className="action-log-link" href={a.event_url} target="_blank" rel="noreferrer"
                 title="view on GitHub ↗">{verb} ↗</a>
            : verb;
          return (
            <li key={`${a.at}-${i}`} title={[a.detail, a.at].filter(Boolean).join(" · ")}>
              <b>{a.identity ?? botLogin}</b> {verbEl} this
              {a.operator && <span className="muted"> · by {a.operator}</span>}
              <span className="muted"> · {timeAgo(a.at)}</span>
              {a.reason && a.reason !== "manual" &&
                <span className="action-log-reason"> · {a.reason}</span>}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
