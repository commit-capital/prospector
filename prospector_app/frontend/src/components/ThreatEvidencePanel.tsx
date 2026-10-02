import { useEffect, useState } from "react";
import { api, type ThreatEvidence } from "../api";
import { evidenceLine } from "../threatEvidence";

/** The preserved evidence of a PR flagged malicious: one line per capture with
 *  its download, or a note that none is captured yet. Refetches whenever
 *  `refresh` changes (the page reloads the PR after a threat scan). */
export function ThreatEvidencePanel({ prNum, refresh, onCapture }: {
  prNum: number; refresh?: unknown; onCapture?: () => void;
}) {
  const [items, setItems] = useState<ThreatEvidence[] | null>(null);
  useEffect(() => {
    api.prEvidence(prNum).then((r) => setItems(r.items)).catch(() => setItems([]));
  }, [prNum, refresh]);
  if (items === null) return null;
  if (!items.length) {
    return (
      <div className="co-detail">
        Evidence not captured yet.{" "}
        {onCapture && <><button className="link-btn" onClick={onCapture}>Run the threat scan</button> on this PR to capture it.</>}
      </div>
    );
  }
  return (
    <ul className="co-paths">
      {items.map((e) => {
        const line = evidenceLine(e);
        return (
          <li key={e.id}>
            Evidence preserved {new Date(e.captured_at).toLocaleString()} · {line.state} · <code>{line.head}</code>
            {line.replaced && <> · force-pushed {line.replaced.at ? new Date(line.replaced.at).toLocaleDateString() : ""} (was <code>{line.replaced.before}</code>)</>}
            {" · "}<a href={api.prEvidenceBundleUrl(prNum, e.id)} download>Download evidence</a>
          </li>
        );
      })}
    </ul>
  );
}
