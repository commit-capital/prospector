import { useState } from "react";
import { api, type FeatureDecision } from "../api";
import { useRepoMeta } from "../RepoMetaContext";

// A maintainer's call on a feature request. A 409 shows its reason here and
// records nothing.
export function FeatureDecisionButtons({ issue, decision, onDone }: {
  issue: number;
  decision?: FeatureDecision | null;
  onDone: () => void;
}) {
  const { meta } = useRepoMeta();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const run = async (d: "implement" | "decline" | "reopen") => {
    setBusy(true);
    setError(undefined);
    try {
      await api.featureDecision(issue, d);
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="feature-decision">
      {decision?.decision === "decline" ? (
        <button className="btn-secondary sm" disabled={busy} onClick={() => run("reopen")}
          title="Clear the decline so this request is considered again">
          Reopen
        </button>
      ) : decision?.decision === "implement" ? (
        <span className="muted small">Implementation requested by {decision.by}.</span>
      ) : (
        <>
          <button className="btn-primary sm" disabled={busy} onClick={() => run("implement")}
            title="Queue an implementation attempt for this feature">
            Implement this feature
          </button>
          <button className="btn-secondary sm" disabled={busy} onClick={() => run("decline")}
            title="Record that this does not belong in the repository">
            Not for {meta?.display_name ?? "this repository"}
          </button>
        </>
      )}
      {error && <span className="small" style={{ color: "var(--red)" }}>{error}</span>}
    </div>
  );
}
