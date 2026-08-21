import { useEffect, useState } from "react";
import { api, type SuggestedAction, type SuggestedActionView } from "../api";
import { fmtEstimate } from "../fmtEstimate";
import { useJobStream } from "../useJobStream";

// One suggested job: what it is, why now, and a Run button that streams the
// job's output tail while it goes. Reattaches to a run of the same kind
// already going (started here, on another tab, or from the Control panel).
function ActionCard({ a, onFinished }: { a: SuggestedAction; onFinished: () => void }) {
  const { log, running, start } = useJobStream(a.kind, {}, () => onFinished());
  const est = fmtEstimate(a.estimate_seconds);
  const run = () => {
    const params = a.count != null ? `?count=${a.count}` : "";
    start(`/api/jobs/run/${a.kind}${params}`);
  };
  const tail = log.length > 0 ? log[log.length - 1] : null;
  return (
    <div className="suggest-card">
      <span className="suggest-card-text">
        <b>{a.title}</b>
        <span className="muted small"> — {a.reason}{est ? ` · takes ${est}` : ""}</span>
        {running && tail && <span className="muted small suggest-tail">{tail}</span>}
      </span>
      <button className="btn-secondary sm" disabled={running} onClick={run}
        title="Run this job now — same run as the Control tab's button, streamed here.">
        {running ? "Running…" : "▶ Run now"}
      </button>
    </div>
  );
}

/** The view's "worth running now" pipeline actions, inline where their output
 *  shows. Renders nothing when the view's data is fresh and fully covered.
 *  `onActionDone` fires when a job finishes — a hook for the host view to
 *  reload its data; the bar refetches its own suggestions alongside. */
export function SuggestedActions({ view, onActionDone }: {
  view: SuggestedActionView;
  onActionDone?: () => void;
}) {
  const [items, setItems] = useState<SuggestedAction[] | null>(null);
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    let active = true;
    api.suggestedActions(view)
      .then((d) => { if (active) setItems(d.items); })
      .catch(() => { if (active) setItems([]); });
    return () => { active = false; };
  }, [view, generation]);

  if (!items || items.length === 0) return null;
  const finished = () => {
    setGeneration((g) => g + 1);
    onActionDone?.();
  };
  return (
    <div className="suggest-bar">
      {items.map((a) => <ActionCard key={a.kind} a={a} onFinished={finished} />)}
    </div>
  );
}
