import type { IssueDetail } from "../api";
import { FeatureDecisionButtons } from "./FeatureDecisionButtons";
import { FeatureMockup } from "./FeatureMockup";
import { FlyoutIssueLink } from "./FlyoutIssueLink";

const VERDICT_CHIP = { consider: "chip-green", "not-a-fit": "chip-muted", unclear: "chip-amber" } as const;

// The maintainer's decision buttons on every feature request, screened or not,
// with the current screen when there is one.
export function FeatureScreenPanel({ d, onChanged }: { d: IssueDetail; onChanged: () => void }) {
  const screen = d.feature_screen;
  const decision = d.feature_decision;
  if (!d.is_feature_request && !screen && !decision) return null;
  return (
    <section className="panel">
      <div className="panel-head"><h3>Feature request</h3></div>
      {screen && (
        <>
          <div className="metaline">
            <span className={`chip sm ${VERDICT_CHIP[screen.verdict]}`}>{screen.verdict}</span>
            {screen.size && <span className="chip chip-muted sm">{screen.size}</span>}
          </div>
          {screen.why && <p>{screen.why}</p>}
          {screen.precedents.length > 0 && (
            <div className="muted small">
              precedents: {screen.precedents.map((n, i) => <span key={n}>{i > 0 && " "}<FlyoutIssueLink n={n} /></span>)}
            </div>
          )}
          {screen.mockup_refused && (
            <div className="muted small">Mockup not shown: {screen.mockup_refused}</div>
          )}
          {screen.has_mockup && <FeatureMockup issue={d.number} title={d.title ?? `#${d.number}`} />}
        </>
      )}
      {decision && (
        <div className="muted small">
          {decision.decision} · {decision.by} · {decision.at.slice(0, 10)}
          {decision.guidance && <> · {decision.guidance}</>}
        </div>
      )}
      <FeatureDecisionButtons issue={d.number} decision={decision} onDone={onChanged} />
    </section>
  );
}
