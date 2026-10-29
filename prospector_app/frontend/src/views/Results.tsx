import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api, type LaneResults, type ResultCase, type ResultGroup } from "../api";
import { Collapsible } from "../components/Collapsible";

const LANES: Record<string, string> = {
  "issue-fix": "Issue fix",
  "issue-followup": "Fix PR follow-up",
  "design-review": "Design review",
  "security-review": "Security review",
  verify: "Test run",
  autofix: "PR autofix",
};

/** Each stage in plain words (pipeline/outcomes.py STAGES). */
const STAGES: Record<string, string> = {
  fixed: "fixed, but only part of the report",
  reproduction: "couldn't reproduce the defect",
  readings: "the candidates read the report differently",
  "no-fix": "no candidate found a fix",
  "not-a-defect": "the report isn't a defect",
  proof: "the fix's own tests didn't prove it",
  compile: "the fix didn't compile",
  suite: "the fix broke the test suite",
  review: "our reviewer rejected the fix",
  revision: "a revision lost its reproductions",
  guardrail: "a guardrail refused the change",
  declined: "stopped before finishing",
  sandbox: "the sandbox broke",
  agent: "the agent was unavailable",
  base: "the base doesn't build",
  ci: "CI still failed after revisions",
  "ci-outside": "CI fails outside the change",
  reviewer: "a code reviewer kept objecting",
  maintainer: "a maintainer's change didn't land",
  fixer: "the fixer couldn't make the change asked for",
  blocking: "found blocking design problems",
  coverage: "couldn't measure test coverage",
  "no-diff": "couldn't fetch the PR's diff",
  error: "the run crashed",
  yellow: "found YELLOW security findings",
  red: "found RED security findings",
  held: "a lens didn't finish, so no verdict was trusted",
  "not-verified": "the test run didn't verify the fix",
  regressed: "the change broke a test that passed before",
  escalate: "the test run needs a person to judge it",
  "unverifiable-no-test": "the PR has no test to run",
  "unverifiable-needs-live-agent": "the test needs a live agent",
  "deps-touched": "the PR changes dependencies, which the sandbox never installs",
  "needs-rebase": "the PR needs a rebase before it can run",
  "refused-safety": "refused to run the PR's code",
  "no-base": "no pinned base on this machine",
  "fetch-error": "couldn't fetch the PR",
  "agent-failed": "the agent failed",
  "agent-unavailable": "the agent was unavailable",
  "sandbox-error": "the sandbox broke",
  "base-lane": "the base fails its own tests",
  interrupted: "the worker restarted mid-run",
  hold: "a sandbox phase errored, so no verdict was trusted",
  exception: "the run crashed",
  refused: "refused the change",
  "run-failed": "the run failed on the machine",
};

const FAULTS: Record<string, { label: string; tone: "red" | "yellow" | "muted" }> = {
  machine: { label: "Prospector's machine", tone: "red" },
  subject: { label: "the report or PR", tone: "muted" },
  judged: { label: "judged", tone: "yellow" },
};

const PARKED: Record<string, string> = {
  rival: "another PR covers what the fix misses",
  boundary: "it crosses a trust boundary",
  single: "only one candidate reproduces the defect",
  intake: "the report didn't read as clear",
  "handed-back": "its PR was handed back",
  partial: "it fixes only part of the report",
  ready: "ready for your review",
};

function issueLink(n: number) {
  return <Link to={`/issues?issue=${n}`}>#{n}</Link>;
}

function CaseRow({ c }: { c: ResultCase }) {
  return (
    <li style={{ marginBottom: 10 }}>
      <div className="small">
        {c.issue != null && <>issue {issueLink(c.issue)} · </>}
        {c.pr != null && <>PR #{c.pr} · </>}
        <span className="muted">{c.at.slice(0, 10)} · {c.trigger}</span>
        {c.by && <> · <b>{c.by}</b></>}
      </div>
      {c.asked && (
        <blockquote className="small muted" style={{ whiteSpace: "pre-wrap", margin: "4px 0" }}>
          asked by <b>{c.asked.by}</b>: {c.asked.words}
        </blockquote>
      )}
      <div style={{ whiteSpace: "pre-wrap" }}>{c.words}</div>
      {c.leaves.length > 0 && (
        <div className="small">leaves: {c.leaves.join("; ")}</div>
      )}
    </li>
  );
}

function GroupBlock({ g }: { g: ResultGroup }) {
  const fault = g.fault ? FAULTS[g.fault] : null;
  return (
    <Collapsible tone={fault?.tone} summary={
      <span>
        <b>{g.count}</b> · {LANES[g.lane] ?? g.lane}: {STAGES[g.stage] ?? g.stage}
        {fault && <span className="muted small"> · {fault.label}</span>}
      </span>
    }>
      <ul>{g.cases.map((c) => <CaseRow key={`${c.at}-${c.issue}-${c.pr}`} c={c} />)}</ul>
      {g.count > g.cases.length && (
        <p className="muted small">and {g.count - g.cases.length} older</p>
      )}
    </Collapsible>
  );
}

/** Where the lanes' attempts stopped, grouped so a failure that keeps
 *  recurring stands out, with each case's own words side by side. */
export default function Results() {
  const [days, setDays] = useState(30);
  const [results, setResults] = useState<LaneResults | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.laneResults(days).then(setResults)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [days]);

  if (error) return <div className="pad"><p className="chip chip-red">{error}</p></div>;
  if (!results) return <div className="pad muted">reading the runs log…</div>;

  return (
    <div className="pad">
      <h2>Results</h2>
      <p className="muted small" style={{ maxWidth: 720 }}>
        Every attempt in the last{" "}
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {[7, 30, 90, 365].map((d) => <option key={d} value={d}>{d}</option>)}
        </select>{" "}
        days ({results.attempts}), grouped by where it stopped. A stop on
        Prospector&rsquo;s machine is ours to fix; a judged stop is a verdict on the work.
      </p>

      <h3>By week</h3>
      <table className="trust-table">
        <thead>
          <tr><th>Week of</th><th>Succeeded</th><th>Machine</th><th>Report or PR</th><th>Judged</th></tr>
        </thead>
        <tbody>
          {results.weeks.map((w) => (
            <tr key={w.week}>
              <td>{w.week}</td><td>{w.ok}</td><td>{w.machine}</td><td>{w.subject}</td><td>{w.judged}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Where attempts stopped</h3>
      {results.groups.length === 0 ? <p className="muted">No stops in this window.</p>
        : results.groups.map((g) => <GroupBlock key={`${g.lane}-${g.stage}`} g={g} />)}

      <h3>Repeats</h3>
      {results.repeats.length === 0 ? <p className="muted">Nothing stopped the same way twice.</p> : (
        <ul>
          {results.repeats.map((r) => (
            <li key={`${r.lane}-${r.stage}-${r.kind}-${r.number}`}>
              <b>{r.count}×</b> {STAGES[r.stage] ?? r.stage} on{" "}
              {r.kind === "pr" ? `PR #${r.number}` : issueLink(r.number)}
              {r.kind === "pr" && r.issue != null && <> (issue {issueLink(r.issue)})</>}
              <span className="muted small"> · last {r.last.slice(0, 10)}</span>
            </li>
          ))}
        </ul>
      )}

      <h3>Parked for you now</h3>
      {results.parked.length === 0 ? <p className="muted">No fix is waiting on you.</p>
        : results.parked.map((p) => (
          <Collapsible key={p.kind} summary={<span><b>{p.count}</b> · {PARKED[p.kind] ?? p.kind}</span>}>
            <ul>
              {p.cases.map((c) => (
                <li key={c.issue}>
                  {issueLink(c.issue)} {c.title}
                  <div className="muted small">{c.why}</div>
                </li>
              ))}
            </ul>
          </Collapsible>
        ))}
    </div>
  );
}
