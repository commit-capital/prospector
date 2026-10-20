import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type IssueDetail, type IssueFixFollowup, type IssueFixRun, type IssuePR } from "../../api";
import { Collapsible } from "../Collapsible";
import { PRLink } from "../PRLink";
import { GitHubPRLink } from "../GitHubPRLink";
import { FlyoutIssueLink } from "../FlyoutIssueLink";
import { IssueActionBar } from "../IssueActionBar";
import { DispositionChip, ReproChip } from "../../views/Issues";
import { EVIDENCE, REFERENCED, linkStateChip } from "./issueLinkChips";

type Tone = "green" | "amber" | "red" | "blue" | "purple" | "gold" | "muted";
type Fact = { tone: Tone; text: string; tip: string };

const TIER_TONE: Record<number, Tone> = { 0: "red", 1: "amber" };
const AUTHOR: Record<string, Fact> = {
  prospector: { tone: "purple", text: "🤖 Prospector autofix", tip: "Written and proposed by Prospector's issue-fix lane." },
  maintainer: { tone: "gold", text: "Maintainer", tip: "Opened by a maintainer of the repository." },
  community: { tone: "muted", text: "Community", tip: "Opened by a community contributor." },
};

function tierFact(tier: number | null | undefined): Fact | null {
  if (tier == null) return null;
  return { tone: TIER_TONE[tier] ?? "green", text: `T${tier}`,
           tip: "How central the changed files are: tier 0 is core, tier 3 a leaf." };
}

function checksFact(p: IssuePR): Fact | null {
  const c = p.facts?.checks;
  if (!c || !c.total) return null;
  return { tone: c.failed ? "red" : c.passed < c.total ? "amber" : "green", text: `${c.passed}/${c.total} checks`,
           tip: "Prospector checks passed out of the checks that ran on this PR." };
}

/** Where our proposed PR stands: merged or closed upstream, else the follow-up's state. */
function statusFact(p: IssuePR, followup: IssueFixFollowup | null, rationale: string | null): Fact | null {
  if (p.state === "merged") return { tone: "purple", text: "Merged", tip: "Merged upstream." };
  if (p.state === "closed") return { tone: "muted", text: "Closed", tip: "Closed upstream without merging." };
  if (!followup) return null;
  const why = followup.judged?.reason ?? followup.reason ?? "";
  if (followup.state === "handed-back") {
    return { tone: "amber", text: "Needs review", tip: [rationale, why && `Follow-up: ${why}.`].filter(Boolean).join("\n\n") };
  }
  if (followup.state === "ready") return { tone: "green", text: "Ready to merge", tip: why };
  if (followup.state === "watching") return { tone: "blue", text: "Watching CI", tip: why };
  return null;
}

/** The proof our fix lane gathered, one fact per kind of evidence. */
function proofFacts(run: IssueFixRun): Fact[] {
  const out: Fact[] = [];
  const missed = run.reviews.flatMap((r) => r.coverage ?? []).filter((c) => c.status === "not-addressed");
  if (missed.length) out.push({ tone: "amber", text: "Partial fix",
    tip: `Not addressed:\n${missed.map((c) => `• ${c.defect}`).join("\n")}` });
  const reps = run.agreement?.reproductions?.length ?? 0;
  if (reps) out.push({ tone: "green", text: "Repro ✓",
    tip: `${reps} independent reproduction${reps === 1 ? "" : "s"} fail on the base and pass with the fix.` });
  const { compile, suite } = run.proof ?? {};
  const newFailures = suite?.new_failures ?? [];
  if (compile?.exit != null && compile.exit !== 0) {
    out.push({ tone: "red", text: "Compile ✗", tip: compile.excerpt ?? "The compile command fails with the fix." });
  } else if (newFailures.length) {
    out.push({ tone: "red", text: "Tests ✗", tip: `New failures: ${newFailures.slice(0, 5).join(", ")}` });
  } else if (suite && !suite.skipped) {
    out.push({ tone: "green", text: "Tests ✓", tip: "It compiles and the full suite shows no new failures"
      + (suite.excluded ? ` (${suite.excluded} files already fail on the base).` : ".") });
  }
  const crossings = run.boundary?.crossings ?? [];
  const concerns = run.reviews.flatMap((r) => r.concerns);
  const safety = run.reviews.find((r) => r.lens === "scope-safety");
  if (crossings.length) {
    out.push({ tone: "amber", text: "Trust boundary", tip: crossings.map((c) => `${c.kind}: ${c.what}`).join("\n") });
  } else if (concerns.length) {
    out.push({ tone: "amber", text: "Review concerns", tip: concerns.join("\n") });
  } else if (safety?.verdict === "safe") {
    out.push({ tone: "green", text: "Safe ✓", tip: safety.reason ?? "The scope-safety review found nothing concerning." });
  }
  const tier = tierFact(run.tier?.tier);
  if (tier) out.push(tier);
  const tried = run.candidates.length;
  const agreed = run.agreement?.agreed?.length ?? 0;
  if (tried > 1) out.push({ tone: agreed === tried ? "green" : "amber", text: `${agreed}/${tried} fixes pass`,
    tip: "Independent fix attempts that passed every reproduction. The smallest one shipped." });
  return out;
}

function FactChips({ facts }: { facts: Fact[] }) {
  return <>{facts.map((f) => <span key={f.text} className={`chip chip-${f.tone} sm`} title={f.tip}>{f.text}</span>)}</>;
}

/** One PR that may fix the issue: who wrote it, then a line of short facts. */
function FixPRRow({ p, d }: { p: IssuePR; d: IssueDetail }) {
  const run = d.fix_run ?? null;
  const ours = run?.proposal?.pr != null && String(run.proposal.pr) === String(p.pr);
  const kind = ours ? "prospector" : p.facts?.author_kind ?? null;
  const followup = ours && d.fix_followup && String(d.fix_followup.pr) === String(p.pr) ? d.fix_followup : null;
  const facts: Fact[] = [];
  if (ours && run) {
    const status = statusFact(p, followup, d.analysis?.rationale ?? null);
    if (status) facts.push(status);
    facts.push(...proofFacts(run));
  } else {
    const state = linkStateChip(p);
    if (state) facts.push({ tone: state.cls === "chip-purple" ? "purple" : "muted", text: state.label, tip: "Current state on GitHub" });
    const tier = tierFact(p.facts?.risk_tier);
    if (tier) facts.push(tier);
    if (p.facts?.disposition) facts.push({ tone: "blue", text: `Suggestion: ${p.facts.disposition}`,
      tip: "Prospector's suggested disposition for this PR." });
  }
  const checks = checksFact(p);
  if (checks) facts.push(checks);
  return (
    <div className="fix-pr-row">
      <div className="fix-pr-head">
        {p.in_store ? <PRLink n={p.pr} className="pr-ref" /> : <GitHubPRLink n={p.pr} className="pr-ref" />}
        {kind && <FactChips facts={[AUTHOR[kind]]} />}
        <span className="fix-pr-title" title={EVIDENCE[p.how ?? ""]}>{p.title}</span>
      </div>
      {facts.length > 0 && <div className="fix-pr-facts"><FactChips facts={facts} /></div>}
    </div>
  );
}

/** The PRs with evidence that they fix the issue, our own first. */
function FixPRs({ d }: { d: IssueDetail }) {
  const ours = d.fix_run?.proposal?.pr;
  const strong = d.linked_prs.filter((p) => REFERENCED.has(p.how ?? ""))
    .sort((a, b) => Number(String(b.pr) === String(ours)) - Number(String(a.pr) === String(ours)));
  const weak = d.linked_pr_count - strong.length;
  if (!strong.length && !weak) return <span className="muted">None yet.</span>;
  return (
    <div className="fix-prs">
      {strong.map((p) => <FixPRRow key={p.pr} p={p} d={d} />)}
      {weak > 0 && (
        <div className="muted small" title="PRs that only share the issue's subsystem, or name it without claiming to fix it">
          +{weak} weakly linked
        </div>
      )}
    </div>
  );
}

/** The bug and the fix it needs, in a sentence each. ANALYZE writes the card;
 *  until it does, the analysis gist stands in for the bug. */
function IssueStory({ d, onWritten }: { d: IssueDetail; onWritten: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const card = d.display;

  const write = () => {
    setBusy(true); setErr(undefined);
    api.writeIssueDisplay(d.number)
      .then(onWritten)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false));
  };

  const bug = card?.bug ?? d.analysis?.gist;
  return (
    <>
      <dl className="pr-story">
        {bug && <><dt>The bug</dt><dd>{bug}</dd></>}
        <dt>Suggested fix</dt>
        <dd>
          {card?.fix ?? (busy
            ? <span className="muted small"><span className="spinner" /> Writing a plain-language summary…</span>
            : <button className="btn-secondary sm" onClick={write}
                title="Have an agent write the bug and the fix it needs in a sentence each.">
                {err ? "Try again" : "Write summary"}
              </button>)}
        </dd>
      </dl>
      {err && <div className="sug-verify" role="alert">⛔ {err}</div>}
    </>
  );
}

/** "Prospector analysis:" — the triage verdict's chips, then its reasoning. */
function IssueAnalysis({ d }: { d: IssueDetail }) {
  const a = d.analysis;
  if (!a) return <div className="muted small">Not yet analyzed. Run Issue analysis from the Control tab.</div>;
  return (
    <div className="issue-analysis">
      <div className="analysis-line">
        <span className="analysis-label">Prospector analysis:</span>
        <DispositionChip d={a.disposition} />
        <ReproChip grade={d.repro_grade} />
        {a.canonical != null && <span className="muted small">duplicate of <FlyoutIssueLink n={a.canonical} /></span>}
        {a.fixed_by != null && <span className="muted small">fixed by <PRLink n={a.fixed_by} /></span>}
      </div>
      {a.rationale && <div className="issue-rationale">{a.rationale}</div>}
      {a.asks && a.asks.length > 0 && (
        <>
          <div className="muted small">Asks for the reporter:</div>
          <ul className="issue-asks">{a.asks.map((ask, i) => <li key={i}>{ask}</li>)}</ul>
        </>
      )}
    </div>
  );
}

function suggestedAction(d: IssueDetail): string {
  const a = d.analysis;
  switch (a?.disposition) {
    case "close-dup": return `Close as a duplicate of #${a.canonical}`;
    case "close-fixed": return a.fixed_by ? `Close as already fixed by #${a.fixed_by}` : "Close as already fixed";
    case "request-repro": return "Ask the reporter for a reproduction";
    case "link-pr": return "Keep open: a linked PR addresses it";
    case "needs-human": return "Needs your decision";
    default: return "Not yet analyzed";
  }
}

/** The issue page's Summary panel: the bug and its fix, the report, the PRs
 *  that may fix it, our analysis, and the one action surface. */
export function IssueSummary({ d, onChanged }: { d: IssueDetail; onChanged: () => void }) {
  return (
    <section className="suggestion summary-panel tone-muted">
      <h3 className="summary-title">Summary</h3>
      <IssueStory d={d} onWritten={onChanged} />
      {d.body && (
        <Collapsible className="summary-description" summary={<>Full issue report</>}>
          <div className="markdown prbody-md" style={{ maxHeight: "none" }}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{d.body}</ReactMarkdown>
          </div>
        </Collapsible>
      )}
      <div>
        <div className="analysis-label">PRs that may fix it</div>
        <FixPRs d={d} />
      </div>
      <IssueAnalysis d={d} />
      <div className="summary-action">
        <div className="sug-headline"><span className="analysis-label">Suggested action:</span> <b>{suggestedAction(d)}</b></div>
        <IssueActionBar d={d} onActed={onChanged} />
      </div>
    </section>
  );
}
