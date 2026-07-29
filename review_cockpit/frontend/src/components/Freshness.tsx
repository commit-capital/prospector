import type { Divergence } from "../api";
import type { FreshnessState } from "../useFreshness";

/** Header indicator for the background live-state refresh (#25). */
export function FreshnessBar({ f }: { f: FreshnessState }) {
  if (f.status === "idle") return null;
  if (f.status === "loading") return <span className="fresh-bar muted small"><span className="spinner" /> checking GitHub for changes…</span>;
  if (f.status === "error") return <span className="fresh-bar muted small">⚠ couldn't refresh live state</span>;
  return (
    <span className={`fresh-bar small ${f.changed ? "fresh-changed" : "muted"}`}>
      {f.changed
        ? <>⚠ {f.changed} PR{f.changed === 1 ? "" : "s"} changed upstream · refreshed {f.at}</>
        : <>✓ Refreshed at {f.at} — no upstream changes</>}
    </span>
  );
}

/** Loud per-PR callout listing what diverged since the snapshot. When the head
 *  itself moved (new commits), offers a "Re-run Analysis" action right there —
 *  the fix for a warning belongs next to the warning, not buried in a panel
 *  further down the page (#582). `onRerunAnalysis` is omitted (button hidden)
 *  when the PR isn't clustered yet, since re-analysis is cluster-scoped. */
export function FreshnessCallout({ diverged, onRerunAnalysis, rerunning, rerunLog }: {
  diverged?: Divergence[];
  onRerunAnalysis?: () => void;
  rerunning?: boolean;
  rerunLog?: string[];
}) {
  if (!diverged?.length) return null;
  const needsReanalysis = diverged.some((d) => d.kind === "head");
  return (
    <div className="fresh-callout" role="alert">
      {diverged.map((d, i) => <div key={i}>⚠ {d.message}</div>)}
      {needsReanalysis && onRerunAnalysis && (
        <button className="btn-secondary sm" style={{ marginTop: 6 }} onClick={onRerunAnalysis} disabled={rerunning}
          title="Refresh this PR's facts and re-classify it against its current head — clears this warning.">
          {rerunning ? "Running…" : "↻ Re-run Analysis for this PR"}
        </button>
      )}
      {rerunLog && rerunLog.length > 1 && (
        <div className="joblog" style={{ marginTop: 8 }} aria-label="re-analysis log">
          {rerunLog.map((l, i) => <div key={i} className="logline">{l}</div>)}
        </div>
      )}
    </div>
  );
}
