import { useEffect, useState } from "react";
import { api, type TrustBar, type TrustLadder, type TrustRate, type TrustType } from "../api";

/** What each action type does, for an operator meeting the ladder cold. */
const TYPE_LABELS: Record<string, { label: string; hint: string }> = {
  update: { label: "Branch update", hint: "merge current main into a contributor's PR branch" },
  rebase: { label: "Rebase", hint: "replay a PR's commits onto current main" },
  resolve: { label: "Conflict resolve", hint: "an agent resolves a rebase's merge conflicts" },
  fix: { label: "Fix draft", hint: "an agent authors a change against review findings" },
  describe: { label: "Description rewrite", hint: "an agent rewrites a PR description to the template" },
  "close-dup": { label: "Close as duplicate", hint: "close a PR another PR already covers" },
  "close-fixed": { label: "Close as fixed", hint: "close a PR a merged change already fixed" },
  merge: { label: "Merge", hint: "squash-merge a PR upstream" },
};

const RUNG_HINTS: Record<string, string> = {
  shadow: "the agent decides and logs; a person decides independently",
  "one-click": "the agent pre-fills; a person approves or overrides",
  "auto+undo": "the agent acts; a person reviews afterward and can undo",
  auto: "the agent acts; a person spot-checks",
};

function pct(x: number): string {
  return `${Math.round(x * 100)}%`;
}

function RateCell({ rate, kind }: { rate: TrustRate; kind: "agreement" | "reversal" }) {
  if (rate.n === 0) return <span className="muted small">no evidence</span>;
  return (
    <span title={`${rate.hits} of ${rate.n} judged ${kind === "agreement" ? "decisions" : "outcomes"}`}>
      {pct(rate.rate)} <span className="muted small">of {rate.n}</span>
    </span>
  );
}

/** The bar a type has not met yet — what promotion to the next rung needs —
 *  or nothing when it sits on the top rung. */
function NextBar({ t, ladder }: { t: TrustType; ladder: TrustLadder }) {
  const above = ladder.rungs[ladder.rungs.indexOf(t.rung) + 1];
  const bar: TrustBar | undefined = above ? ladder.bars[above] : undefined;
  if (!above || !bar) return <span className="muted small">top rung</span>;
  return (
    <span className="muted small">
      {above}: ≥{pct(bar.min_agreement)} agreement over ≥{bar.min_decisions} decisions,
      ≤{pct(bar.max_reversal)} reversals
    </span>
  );
}

/** The four rungs with the type's earned one highlighted. */
function RungCell({ t, ladder }: { t: TrustType; ladder: TrustLadder }) {
  return (
    <span className="trust-rungs">
      {ladder.rungs.map((r) => (
        <span key={r} title={RUNG_HINTS[r] ?? r}
          className={r === t.rung ? "chip chip-blue sm" : "chip chip-muted sm trust-rung-off"}>
          {r}
        </span>
      ))}
    </span>
  );
}

export default function Policy() {
  const [ladder, setLadder] = useState<TrustLadder | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.trustLadder().then(setLadder)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  if (error) return <div className="pad"><p className="chip chip-red">{error}</p></div>;
  if (!ladder) return <div className="pad muted">reading decision history…</div>;

  return (
    <div className="pad">
      <h2>📜 Policy — trust ladder</h2>
      <p className="muted small" style={{ maxWidth: 720 }}>
        Each autonomous action type earns its rung from the record: agreement is
        how often a person accepts the agent&rsquo;s pick, reversal is how often the
        automation&rsquo;s work is undone or rejected, both over the last {ladder.window}{" "}
        judged events. The rung is recomputed on every read, so a reversal spike
        demotes a type with no human action. The rung is a disclosure — the
        autopush and hunt switches on each machine&rsquo;s Setup page remain the
        enforcement.
      </p>
      <table className="trust-table">
        <thead>
          <tr>
            <th>Action</th>
            <th>Rung</th>
            <th>Agreement</th>
            <th>Reversal</th>
            <th>Next rung needs</th>
          </tr>
        </thead>
        <tbody>
          {ladder.types.map((t) => {
            const info = TYPE_LABELS[t.id];
            return (
              <tr key={t.id}>
                <td>
                  {info?.label ?? t.id}
                  {info && <div className="muted small">{info.hint}</div>}
                </td>
                <td><RungCell t={t} ladder={ladder} /></td>
                <td><RateCell rate={t.agreement} kind="agreement" /></td>
                <td><RateCell rate={t.reversal} kind="reversal" /></td>
                <td><NextBar t={t} ladder={ladder} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="muted small" style={{ maxWidth: 720 }}>
        Rungs: {ladder.rungs.map((r) => `${r} — ${RUNG_HINTS[r] ?? ""}`).join("; ")}.
      </p>
    </div>
  );
}
