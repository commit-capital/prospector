import type { ReactNode } from "react";
import type { Divergence } from "../api";
import type { FreshnessState } from "../useFreshness";
import { liveConflicts } from "./liveChecks";
import { shortDateTime } from "../timeAgo";

/** Header indicator for the background live-state refresh (#25). `updatedAt`,
 *  the PR's own last update on GitHub, leads it so the refresh time reads as
 *  when Prospector checked the PR, not when the PR changed. */
export function FreshnessBar({ f, updatedAt }: { f: FreshnessState; updatedAt?: string | null }) {
  const updated = shortDateTime(updatedAt);
  const lead = updated
    ? <span className="muted" title={updatedAt ?? undefined}>PR last updated {updated}{f.status === "idle" ? "" : " · "}</span>
    : null;
  if (f.status === "idle") return lead;
  if (f.status === "loading") return <span className="fresh-bar muted small">{lead}<span className="spinner" /> checking GitHub for changes…</span>;
  if (f.status === "error") return <span className="fresh-bar muted small">{lead}⚠ couldn't refresh live state</span>;
  return (
    <span className={`fresh-bar small ${f.changed ? "fresh-changed" : "muted"}`}>
      {lead}
      {f.changed
        ? <>⚠ {f.changed} PR{f.changed === 1 ? "" : "s"} changed upstream · PR data refreshed {f.at}</>
        : <>✓ PR data refreshed at {f.at} — no upstream changes</>}
    </span>
  );
}

/** Loud per-PR callout listing what diverged since the snapshot, leaving out
 *  CI that moved to passing (the reloaded Checks show it). When the head
 *  itself moved (new commits), offers a "Re-run Analysis" action right there —
 *  the fix for a warning belongs next to the warning, not buried in a panel
 *  further down the page (#582). `onRerunAnalysis` is omitted (button hidden)
 *  when the PR isn't clustered yet, since re-analysis is cluster-scoped.
 *  `conflictAction` renders beside a new-conflicts warning. */
export function FreshnessCallout({ diverged, onRerunAnalysis, conflictAction, rerunning, rerunLog }: {
  diverged?: Divergence[];
  onRerunAnalysis?: () => void;
  conflictAction?: ReactNode;
  rerunning?: boolean;
  rerunLog?: string[];
}) {
  const shown = (diverged ?? []).filter((d) => !(d.kind === "ci" && d.now === "passing"));
  if (!shown.length) return null;
  const needsReanalysis = shown.some((d) => d.kind === "head");
  return (
    <div className="fresh-callout" role="alert">
      {shown.map((d, i) => <div key={i}>⚠ {d.message}</div>)}
      {needsReanalysis && onRerunAnalysis && (
        <button className="btn-secondary sm" style={{ marginTop: 6 }} onClick={onRerunAnalysis} disabled={rerunning}
          title="Refresh this PR's facts and re-classify it against its current head — clears this warning.">
          {rerunning ? "Running…" : "↻ Re-run Analysis for this PR"}
        </button>
      )}
      {liveConflicts(shown) && conflictAction && <div style={{ marginTop: 6 }}>{conflictAction}</div>}
      {rerunLog && rerunLog.length > 1 && (
        <div className="joblog" style={{ marginTop: 8 }} aria-label="re-analysis log">
          {rerunLog.map((l, i) => <div key={i} className="logline">{l}</div>)}
        </div>
      )}
    </div>
  );
}
