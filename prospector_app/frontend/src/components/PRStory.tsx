import { useState } from "react";
import { api, type PRDetail } from "../api";

/** The Summary panel's opening lines: what was wrong, what the PR changes, and
 *  how it works. Until a card is written for this head, the diff summary's
 *  one-liner stands in as the fix, with a button to write the card. */
export function PRStory({ pr, onWritten }: { pr: PRDetail; onWritten: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const d = pr.display;
  const fix = d?.fix ?? pr.summary?.one_liner;

  const write = () => {
    setBusy(true); setErr(undefined);
    api.writeDisplay(pr.number)
      .then(onWritten)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false));
  };

  return (
    <>
      {(fix || d) && (
        <dl className="pr-story">
          {d && <><dt>Problem</dt><dd>{d.problem}</dd></>}
          {fix && <><dt>Fix</dt><dd>{fix}</dd></>}
          {d && d.how.length > 0 && (
            <><dt>How it works</dt><dd><ul>{d.how.map((x, i) => <li key={i}>{x}</li>)}</ul></dd></>
          )}
        </dl>
      )}
      {!d && (
        <div>
          <button className="btn-secondary sm" onClick={write} disabled={busy}
            title="Have an agent write a plain-language problem, fix and how-it-works for this PR.">
            {busy ? "Writing…" : "Write summary"}
          </button>
        </div>
      )}
      {err && <div className="sug-verify" role="alert">⛔ {err}</div>}
    </>
  );
}

type Tone = "green" | "amber" | "red" | "muted";
type Item = { tone: Tone; text: string; tip: string };

const TIER_TONE: Record<number, Tone> = { 0: "red", 1: "amber" };
const SAFETY: Record<string, Item> = {
  safe: { tone: "green", text: "🛡️ Likely safe", tip: "The security review found nothing concerning at this head." },
  caution: { tone: "amber", text: "🛡️ Concerns flagged", tip: "The security review flagged concerns — see the banner above." },
  risk: { tone: "red", text: "🛡️ Risky", tip: "The security review found serious problems — see the banner above." },
};

/** The analysis facts as chips, each read from a structured field. */
function analysisItems(pr: PRDetail): Item[] {
  const out: Item[] = [];
  const c = pr.checks;
  if (c && c.total > 0) {
    const failed = c.checks.some((x) => x.status === "fail" && !x.advisory);
    out.push({ tone: failed ? "red" : c.passed < c.total ? "amber" : "green",
               text: `${c.passed}/${c.total} checks`, tip: "Checks passed out of the checks that ran (the Checks table below)." });
  }
  const sec = pr.checks?.checks.find((x) => x.key === "security");
  const level = pr.safety_summary?.level ?? "unknown";
  if (level === "safe" && sec?.stale) {
    out.push({ tone: "amber", text: "🛡️ Safe at an earlier commit", tip: pr.safety_summary?.detail ?? "" });
  } else if (SAFETY[level]) {
    out.push(SAFETY[level]);
  } else {
    out.push({ tone: pr.disposition === "merge" ? "amber" : "muted", text: "🛡️ Not reviewed",
               tip: pr.safety_summary?.detail ?? "No security review has judged this PR." });
  }
  const design = pr.reviews_detail?.design?.digest;
  if (design && design.status !== "na") {
    const open = Object.values(design.open).reduce((a, b) => a + b, 0);
    const major = (design.open.blocking ?? 0) + (design.open.major ?? 0);
    out.push(design.status === "fail"
      ? { tone: "red", text: `${major || open} blocking design finding${(major || open) === 1 ? "" : "s"}`, tip: design.summary_line }
      : design.status === "pass"
        ? open
          ? { tone: "amber", text: `${open} minor design finding${open === 1 ? "" : "s"}`, tip: design.summary_line }
          : { tone: "green", text: "Design clean", tip: design.summary_line }
        : { tone: "muted", text: `Design ${design.status}`, tip: design.summary_line });
  }
  if (pr.risk_tier != null) {
    out.push({ tone: TIER_TONE[pr.risk_tier] ?? "green", text: `Tier ${pr.risk_tier} change`,
               tip: "How central the changed files are: tier 0 is core, tier 3 a leaf." });
  }
  return out;
}

/** "Prospector analysis:" — one line of green/amber/red fact chips. */
export function AnalysisLine({ pr }: { pr: PRDetail }) {
  const items = analysisItems(pr);
  if (!items.length) return null;
  return (
    <div className="analysis-line">
      <span className="analysis-label">Prospector analysis:</span>
      {items.map((it) => <span key={it.text} className={`chip chip-${it.tone} sm`} title={it.tip}>{it.text}</span>)}
    </div>
  );
}
