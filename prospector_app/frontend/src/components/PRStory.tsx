import { useState } from "react";
import { api, type PRDetail } from "../api";
import { LinkedIssues } from "./LinkedIssues";
import { closedNote, shownIssues } from "./linkedIssuesShown";
import { useRepoMeta } from "../RepoMetaContext";

/** The Summary panel's opening lines: what was wrong, what the PR changes (and
 *  the issues it fixes), and how it works. ANALYZE writes the card; until it
 *  does, the diff summary's one-liner stands in as the fix. */
export function PRStory({ pr, onWritten }: { pr: PRDetail; onWritten: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const d = pr.display;
  const fix = d?.fix ?? pr.summary?.one_liner;
  const fixes = shownIssues(pr.issues).shown;

  const write = () => {
    setBusy(true); setErr(undefined);
    api.writeDisplay(pr.number)
      .then(onWritten)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false));
  };

  return (
    <>
      {(fix || d || fixes.length > 0) && (
        <dl className="pr-story">
          {d && <><dt>Problem</dt><dd>{d.problem}</dd></>}
          {(fix || fixes.length > 0) && (
            <><dt>Fix</dt><dd>
              {fix}
              {fixes.length > 0 && <span className="muted"> (Fixes <LinkedIssues issues={pr.issues} />)</span>}
              {fixes.map((issue) => {
                const closed = closedNote(issue);
                return closed && <div key={issue.issue} className="issue-closed-warn">⚠ Issue #{issue.issue} is {closed}.</div>;
              })}
            </dd></>
          )}
          {d && d.how.length > 0 && (
            <><dt>How it works</dt><dd><ul>{d.how.map((x, i) => <li key={i}>{x}</li>)}</ul></dd></>
          )}
        </dl>
      )}
      {!d && busy && <div className="muted small"><span className="spinner" /> Writing a plain-language summary…</div>}
      {!d && !busy && (
        <div>
          <button className="btn-secondary sm" onClick={write}
            title="Have an agent write a plain-language problem, fix and how-it-works for this PR.">
            {err ? "Try again" : "Write summary"}
          </button>
        </div>
      )}
      {err && <div className="sug-verify" role="alert">⛔ {err}</div>}
    </>
  );
}

type Tone = "green" | "amber" | "red" | "muted";
/** Where a chip leads: the Checks panel, one check row (opened), or a diff file. */
export type AnalysisTarget = { checks: true } | { check: string } | { diff: string };
type Item = { tone: Tone; text: string; tip: string; to?: AnalysisTarget };

const TIER_TONE: Record<number, Tone> = { 0: "red", 1: "amber" };
const SAFETY: Record<string, Omit<Item, "to">> = {
  safe: { tone: "green", text: "🛡️ Likely safe", tip: "The security review found nothing concerning at this head." },
  caution: { tone: "amber", text: "🛡️ Concerns flagged", tip: "The security review flagged concerns — see the banner above." },
  risk: { tone: "red", text: "🛡️ Risky", tip: "The security review found serious problems — see the banner above." },
};
const DRIFT: Record<string, (branch: string) => Omit<Item, "to">> = {
  conflicts: (b) => ({ tone: "amber", text: `Conflicts with ${b}`, tip: "Clashes with newer code. The author needs to update it." }),
  "already-fixed": (b) => ({ tone: "red", text: `Already fixed on ${b}`, tip: "This is already fixed in the main code, so the PR is probably not needed." }),
};

/** The analysis facts as chips, each read from a structured field. */
function analysisItems(pr: PRDetail, branch: string): Item[] {
  const out: Item[] = [];
  const c = pr.checks;
  if (c && c.total > 0) {
    const failed = c.checks.some((x) => x.status === "fail" && !x.advisory);
    out.push({ tone: failed ? "red" : c.passed < c.total ? "amber" : "green", to: { checks: true },
               text: `${c.passed}/${c.total} checks`, tip: "Checks passed out of the checks that ran (the Checks table below)." });
  }
  const sec = pr.checks?.checks.find((x) => x.key === "security");
  const level = pr.safety_summary?.level ?? "unknown";
  const toSec = { check: "security" };
  if (level === "safe" && sec?.stale) {
    out.push({ tone: "amber", text: "🛡️ Safe at an earlier commit", tip: pr.safety_summary?.detail ?? "", to: toSec });
  } else if (SAFETY[level]) {
    out.push({ ...SAFETY[level], to: toSec });
  } else {
    out.push({ tone: pr.disposition === "merge" ? "amber" : "muted", text: "🛡️ Not reviewed", to: toSec,
               tip: pr.safety_summary?.detail ?? "No security review has judged this PR." });
  }
  const design = pr.reviews_detail?.design?.digest;
  if (design && design.status !== "na") {
    const open = Object.values(design.open).reduce((a, b) => a + b, 0);
    const major = (design.open.blocking ?? 0) + (design.open.major ?? 0);
    const item: Omit<Item, "to"> = design.status === "fail"
      ? { tone: "red", text: `${major || open} blocking design finding${(major || open) === 1 ? "" : "s"}`, tip: design.summary_line }
      : design.status === "pass"
        ? open
          ? { tone: "amber", text: `${open} minor design finding${open === 1 ? "" : "s"}`, tip: design.summary_line }
          : { tone: "green", text: "Design clean", tip: design.summary_line }
        : { tone: "muted", text: `Design ${design.status}`, tip: design.summary_line };
    out.push({ ...item, to: { check: "design" } });
  }
  // "applicable" is a passing Checks row the checks chip already counts.
  const drift = pr.drift_state ? DRIFT[pr.drift_state] : undefined;
  if (drift) out.push({ ...drift(branch), to: { check: "drift" } });
  if (pr.risk_tier != null) {
    const pinned = pr.risk_tier_paths ?? [];
    const named = pinned.slice(0, 8).join("\n") + (pinned.length > 8 ? `\n+${pinned.length - 8} more` : "");
    out.push({ tone: TIER_TONE[pr.risk_tier] ?? "green", text: `Tier ${pr.risk_tier} change`,
               tip: "How central the changed files are: tier 0 is core, tier 3 a leaf."
                 + (named ? `\n\nTier ${pr.risk_tier} paths:\n${named}` : ""),
               to: pinned.length ? { diff: pinned[0] } : undefined });
  }
  return out;
}

/** "Prospector analysis:" — one line of green/amber/red fact chips, each a
 *  link to the section that says more. */
export function AnalysisLine({ pr, onJump }: { pr: PRDetail; onJump: (to: AnalysisTarget) => void }) {
  const { meta } = useRepoMeta();
  const items = analysisItems(pr, meta?.default_branch ?? "the default branch");
  if (!items.length) return null;
  return (
    <div className="analysis-line">
      <span className="analysis-label">Prospector analysis:</span>
      {items.map((it) => {
        const to = it.to;
        return to
          ? <button key={it.text} type="button" className={`chip chip-${it.tone} sm chip-jump`} title={it.tip}
              onClick={() => onJump(to)}>{it.text}</button>
          : <span key={it.text} className={`chip chip-${it.tone} sm`} title={it.tip}>{it.text}</span>;
      })}
    </div>
  );
}
