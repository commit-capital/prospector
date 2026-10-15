import { useEffect, useState } from "react";
import { api, type PRCommitSummary, type PRHistoryItem, type PRHistoryVia, type PRTimelineItem } from "../api";
import { Collapsible } from "./Collapsible";
import { timeAgo } from "../timeAgo";

const KIND_ICON: Record<PRHistoryItem["kind"], string> = {
  comment: "💬", review: "📝", bot_review: "🔍", commit: "⬆️",
  reopened: "♻️", closed: "🚫", merged: "🔀", force_push: "⏫", renamed: "✏️",
  prospector_action: "🔨",
};

const KIND_VERB: Record<PRHistoryItem["kind"], string> = {
  comment: "commented", review: "reviewed", bot_review: "reviewed",
  commit: "pushed", reopened: "reopened this PR", closed: "closed this PR", merged: "merged this PR",
  force_push: "force-pushed the branch", renamed: "renamed this PR", prospector_action: "",
};

/** "via Prospector · by <operator> · <what it did>" on an entry Prospector made. */
function ViaTag({ via, withVerb }: { via: PRHistoryVia; withVerb: boolean }) {
  const who = via.initiator === "worker" ? "on its own" : via.operator ? `by ${via.operator}` : null;
  const parts = ["via Prospector", who, withVerb ? via.verb : null,
    via.reason && via.reason !== "manual" ? via.reason : null].filter(Boolean);
  return <span className="ph-via" title={via.detail ?? undefined}> · {parts.join(" · ")}</span>;
}

function SummaryRow({ s }: { s: PRCommitSummary }) {
  return (
    <li className="ph-row ph-prospector">
      <span className="ph-icon" aria-hidden="true">🔨</span>
      <span className="ph-body">
        <b>Prospector</b>{s.head && <span className="muted"> (latest commit)</span>}:{" "}
        {s.facts.length === 0
          ? <span className="muted">nothing concluded yet</span>
          : s.facts.map((f, i) => <span key={i} className={`ph-fact tone-${f.tone}`}>{f.label}</span>)}
        {s.unrecorded.length > 0 && (
          <span className="muted small"> · {s.unrecorded.join(", ")} not recorded for this commit</span>
        )}
      </span>
      <span className="muted small ph-time mono" title={s.sha}>{s.sha.slice(0, 7)}</span>
    </li>
  );
}

function EntryRow({ it }: { it: PRHistoryItem }) {
  const own = it.kind === "prospector_action";
  const body = it.kind === "bot_review" && it.score != null ? `confidence ${it.score}/5`
    : own ? it.via?.detail : it.summary;
  return (
    <li className="ph-row">
      <span className="ph-icon" aria-hidden="true">{KIND_ICON[it.kind]}</span>
      <span className="ph-body">
        {it.actor && <b>{it.actor}</b>} {own ? it.via?.verb : KIND_VERB[it.kind]}
        {it.state && it.kind === "review" && <span className="muted"> ({it.state})</span>}
        {body && <span className="ph-text"> — {body}</span>}
        {it.url && (
          <a className="action-log-link" href={it.url} target="_blank" rel="noreferrer" title="view on GitHub ↗"> ↗</a>
        )}
        {it.via && <ViaTag via={it.via} withVerb={!own} />}
      </span>
      <span className="muted small ph-time" title={it.at}>{timeAgo(it.at)}</span>
    </li>
  );
}

/** The PR's single timeline, oldest first: GitHub's comments, reviews,
 *  commits and state events with Prospector's own writes tagged, its writes
 *  GitHub shows no entry for, and a Prospector summary after each commit.
 *  Hidden while loading finds nothing. `refresh` bumps to re-fetch after a new
 *  action. */
export function PRHistory({ pr, refresh = 0 }: { pr: number; refresh?: number }) {
  const [items, setItems] = useState<PRTimelineItem[] | null>(null);

  useEffect(() => {
    let live = true;
    api.prHistory(pr).then((r) => { if (live) setItems(r.items); }).catch(() => { if (live) setItems([]); });
    return () => { live = false; };
  }, [pr, refresh]);

  // a newly selected PR starts empty rather than showing the last one's timeline
  const [shownPr, setShownPr] = useState(pr);
  if (shownPr !== pr) { setShownPr(pr); setItems(null); }

  if (!items || items.length === 0) return null;
  const events = items.filter((it) => it.kind !== "prospector").length;

  return (
    <section className="prc-section">
      <Collapsible defaultOpen summary={<>🕘 History <span className="count">{events}</span></>}>
        <ul className="pr-history-list">
          {items.map((it, i) => it.kind === "prospector"
            ? <SummaryRow key={`p-${it.sha}`} s={it} />
            : <EntryRow key={`${it.at}-${i}`} it={it} />)}
        </ul>
      </Collapsible>
    </section>
  );
}
