import type { IssueDetail, IssueFixDefect, IssueFixRivalEntry } from "../../api";
import { Collapsible } from "../Collapsible";
import { GitHubPRLink } from "../GitHubPRLink";
import { issueSectionId } from "../pageAnchor";
import { rivalRows } from "./rivalComparison";
import { type HoldReason, plainOutput, testFailures } from "./holdReasons";

const DEFECT_CHIP: Record<IssueFixDefect["status"], { cls: string; text: string }> = {
  addressed: { cls: "chip-green", text: "✓ fixed" },
  "not-addressed": { cls: "chip-yellow", text: "✗ not fixed" },
  "already-fixed": { cls: "chip-green", text: "already fixed" },
  "out-of-scope": { cls: "", text: "out of scope" },
};

const RULING: Record<string, string> = {
  handled: "the base already handles it", deliberate: "the behavior is deliberate",
  "outside-report": "it is outside the report", untestable: "no test could reach it",
};

const RIVAL_CHIP: Record<IssueFixRivalEntry["verdict"], { cls: string; text: string }> = {
  covered: { cls: "chip-green", text: "ours covers it" },
  "gap-closed": { cls: "chip-green", text: "ours now covers it" },
  gap: { cls: "chip-red", text: "covers more than ours" },
  "gap-open": { cls: "chip-red", text: "covers more than ours" },
  skipped: { cls: "", text: "not compared" },
};

/** What the host's claim test of a defect showed, unless the evidence already says it. */
function ClaimLine({ d }: { d: IssueFixDefect }) {
  const c = d.claim;
  if (!c || (c.why && d.evidence.includes(c.why))) return null;
  const what = c.status === "shown" ? "shows it is still there"
    : `could not show it is still there${c.ruling ? `: ${RULING[c.ruling] ?? c.ruling}` : ""}`;
  return <div className="muted small">Host's claim test {what}{c.why ? `. ${c.why}` : ""}</div>;
}

function DefectRows({ defects }: { defects: IssueFixDefect[] }) {
  return (
    <div className="defect-rows">
      {defects.map((d) => (
        <div key={d.defect} className="defect-row">
          <span className={`chip sm ${DEFECT_CHIP[d.status].cls}`}>{DEFECT_CHIP[d.status].text}</span>
          <div>
            <div>{d.defect}</div>
            {d.evidence && <div className="muted small">{d.evidence}</div>}
            <ClaimLine d={d} />
          </div>
        </div>
      ))}
    </div>
  );
}

/** One reason the fix is held, with the stored evidence for it. */
function ReasonDetail({ r, d }: { r: HoldReason; d: IssueDetail }) {
  const run = d.fix_run;
  switch (r.kind) {
    case "handed-back": {
      const fu = d.fix_followup;
      return (
        <div>{d.story?.text ?? fu?.judged?.reason ?? fu?.reason}
          <div className="muted small">The fix thread below has the reviewer's words in full.</div></div>
      );
    }
    case "intake": {
      const intake = run?.intake;
      return (
        <>
          {intake?.reason && <div>{intake.reason}</div>}
          {intake?.findings.map((f, i) => (
            <div key={i} className="muted small"><b>{f.kind}:</b> {f.why}
              {f.quote && <blockquote className="held-quote">{f.quote}</blockquote>}</div>
          ))}
        </>
      );
    }
    case "single":
      return (
        <ul className="held-list">
          {run?.candidates.map((c) => (
            <li key={c.index}>{c.reproduces ? "✓" : "✗"} {c.model ?? `candidate ${c.index}`}:{" "}
              {c.reproduces ? "its tests fail on the base, so they reproduce the bug"
                : "its tests do not reproduce the bug"}</li>
          ))}
        </ul>
      );
    case "boundary": {
      const crossings = r.hold?.crossings ?? [];
      if (!crossings.length) return null;
      return (
        <ul className="held-list">
          {crossings.map((c, i) => (
            <li key={i}><b>{c.kind}</b> at <code>{c.where}</code>
              {c.requested ? " (the report asks for it)" : " (the report does not ask for it)"}: {c.what}</li>
          ))}
        </ul>
      );
    }
    case "rival":
      return (
        <div>
          {(run?.second_opinion ?? []).filter((e) => r.hold?.prs?.includes(e.pr))
            .map((e) => <div key={e.pr}><GitHubPRLink n={e.pr} />: {e.why}.</div>)}
          <a href={`#${issueSectionId("rivals")}`}>The comparison is below.</a>
        </div>
      );
    case "partial":
      return <div>Each defect's evidence is below.</div>;
  }
}

/** What a rival's tests reported against our fix: each failing test, the
 *  errors, and the whole run behind a toggle. */
function RivalTests({ output }: { output: string }) {
  const f = testFailures(output);
  return (
    <div className="rival-tests">
      {f.summary && <div className="small">Its tests on our fix: <b>{f.summary}</b></div>}
      {f.tests.length > 0 && (
        <ul className="held-list">{f.tests.map((t) => <li key={t} className="rival-test"><code>{t}</code></li>)}</ul>
      )}
      {f.errors.length > 0 && <pre className="rival-errors">{f.errors.join("\n")}</pre>}
      {!f.tests.length && !f.errors.length && <pre className="rival-errors">{f.tail.join("\n")}</pre>}
      <Collapsible summary={<>Full test output</>}>
        <pre className="rival-output">{plainOutput(output)}</pre>
      </Collapsible>
    </div>
  );
}

function Rivals({ d }: { d: IssueDetail }) {
  const rows = rivalRows(d);
  if (!rows.length) return null;
  return (
    <div className="issue-analysis" id={issueSectionId("rivals")}>
      <div className="analysis-label">Our fix vs. the other open PRs</div>
      {rows.map(({ pr, entry }) => (
        <div key={pr} className="rival">
          <div>
            <GitHubPRLink n={pr} />{entry?.author ? ` by ${entry.author}` : ""}{" "}
            {entry ? <>
              <span className={`chip sm ${RIVAL_CHIP[entry.verdict].cls}`}>{RIVAL_CHIP[entry.verdict].text}</span>{" "}
              <span className="muted">{entry.why}</span>
            </> : <span className="muted">not compared: the fix hasn't been run against its tests</span>}
          </div>
          {entry?.output && <RivalTests output={entry.output} />}
          {(entry?.missed?.length ?? 0) > 0 && (
            <>
              <div className="small">Cases ours misses:</div>
              <ul className="held-list">{entry?.missed?.map((m, i) => <li key={i}>{m}</li>)}</ul>
            </>
          )}
        </div>
      ))}
    </div>
  );
}

/** Why the issue's fix waits on the operator, then what it fixes and what it
 *  doesn't, defect by defect, then how it fares against the other open PRs. */
export function WhyHeld({ d, reasons }: { d: IssueDetail; reasons: HoldReason[] }) {
  const cov = d.fix_coverage;
  const defects = cov && cov.state !== "unchecked" ? cov.defects : [];
  return (
    <>
      {reasons.length > 0 && (
        <div className="issue-analysis">
          <div className="analysis-label">Why it's held</div>
          <ol className="held-reasons">
            {reasons.map((r) => <li key={r.kind}><b>{r.text}.</b> <ReasonDetail r={r} d={d} /></li>)}
          </ol>
        </div>
      )}
      {reasons.length > 0 && defects.length > 0 && (
        <div className="issue-analysis">
          <div className="analysis-label">What our fix handles, by the reviewers' account</div>
          <DefectRows defects={defects} />
          {(cov?.unshown.length ?? 0) > 0 && (
            <>
              <div className="muted small">Claimed left, but no claim test could show it:</div>
              <DefectRows defects={cov?.unshown ?? []} />
            </>
          )}
        </div>
      )}
      <Rivals d={d} />
    </>
  );
}
