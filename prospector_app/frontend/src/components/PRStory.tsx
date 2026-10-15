import { useState } from "react";
import { api, type PRDetail } from "../api";
import { dispositionEntry } from "../glossary";
import { Collapsible } from "./Collapsible";

/** The card's opening lines: what was wrong and what the PR changes. Until a
 *  card is written for this head, the diff summary's one-liner stands in as
 *  the fix. */
export function ProblemFix({ pr }: { pr: PRDetail }) {
  const d = pr.display;
  const fix = d?.fix ?? pr.summary?.one_liner;
  if (!fix) return null;
  return (
    <dl className="pr-story">
      {d && <><dt>Problem</dt><dd>{d.problem}</dd></>}
      <dt>Fix</dt><dd>{fix}</dd>
    </dl>
  );
}

/** Why the verdict and how the change works, collapsed. Until a card is
 *  written it shows the rationale as recorded, with a button to write one. */
export function StoryDetails({ pr, showWhy, onWritten }:
  { pr: PRDetail; showWhy: boolean; onWritten: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const d = pr.display;
  const rationale = pr.proposed_action?.rationale;
  const verdict = (dispositionEntry(pr.disposition)?.title ?? pr.disposition ?? "").toLowerCase();

  const write = () => {
    setBusy(true); setErr(undefined);
    api.writeDisplay(pr.number)
      .then(onWritten)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false));
  };

  return (
    <Collapsible className="pr-story-details" summary={<>Details</>}
      action={!d && (
        <button className="btn-secondary sm" onClick={write} disabled={busy}
          title="Have an agent write a plain-language problem, fix, why and how for this PR.">
          {busy ? "Writing…" : "Write summary"}
        </button>
      )}>
      {d ? (
        <>
          {showWhy && verdict && <Bullets head={`Why ${verdict}`} items={d.why} />}
          <Bullets head="How it works" items={d.how} />
        </>
      ) : rationale
        ? <div className="pr-story-rationale">{rationale}</div>
        : <div className="muted small">No detail yet.</div>}
      {err && <div className="sug-verify" role="alert">⛔ {err}</div>}
    </Collapsible>
  );
}

function Bullets({ head, items }: { head: string; items: string[] }) {
  return (
    <div className="pr-story-part">
      <div className="pr-story-head">{head}</div>
      <ul>{items.map((x, i) => <li key={i}>{x}</li>)}</ul>
    </div>
  );
}
