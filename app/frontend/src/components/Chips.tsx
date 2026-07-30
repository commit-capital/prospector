import { useState } from "react";
import type { Safety, SafetyRollup, AuthorStats, HumanMerge, PRResponseAck } from "../api";
import { InfoTip } from "./InfoTip";
import { term } from "../glossary";

/** Shown anywhere a merge candidate is listed when the PR no longer merges
 *  cleanly — the merge gate blocks it, so surface it loudly (#189). */
export function ConflictChip({ live = false }: { live?: boolean }) {
  return (
    <span className="chip chip-red sm" title={live
      ? "Now has merge conflicts (was clean when this cluster was analyzed). The author needs to rebase before it can merge."
      : "Has merge conflicts — the branch no longer merges cleanly onto the default branch. It can't be merged until the author rebases."}>
      ⚠ merge conflicts
    </span>
  );
}

/** Compact flag that a PR is CODEOWNERS-gated and can't be bot-merged — shown
 *  in tables, not just the PR detail page (#71). */
export function HumanMergeChip({ hm }: { hm?: HumanMerge | null }) {
  if (!hm?.required) return null;
  const paths = (hm.paths || []).slice(0, 8);
  const tip = "Requires a human code-owner to merge — CODEOWNERS-gated paths:\n"
    + paths.join("\n") + (hm.paths && hm.paths.length > 8 ? "\n…" : "")
    + (hm.owners?.length ? `\n\nOwners: ${hm.owners.join(", ")}` : "");
  return <span className="chip chip-red sm" title={tip}>⛔ human merge</span>;
}

/** A PR opened as a draft — never a merge candidate; only close / request-changes
 *  actions apply. Surfaced wherever PRs are listed so drafts are obvious (#224). */
export function DraftChip({ draft }: { draft?: boolean }) {
  if (!draft) return null;
  return (
    <span className="chip chip-muted sm" title={
      "Draft PR — the author has not marked it ready for review. It can never be "
      + "merge-recommended or merged; only close (dup / stale / fixed) or "
      + "request-changes actions apply."}>
      ✎ draft
    </span>
  );
}

export interface SafetyPR { pr: number; verdict: string; fresh?: boolean | null; title: string | null; gating?: boolean; findings: { severity: string; title: string }[] }

export function SafetyChip({ v, findings }: { v: Safety; findings?: { severity: string; title: string }[] }) {
  if (!v) return <span className="chip chip-muted" title="No safety verdict yet — run /diagnose-pr-cluster on its cluster.">no verdict</span>;
  const cls = { GREEN: "chip-green", YELLOW: "chip-yellow", RED: "chip-red" }[v];
  const dot = { GREEN: "🟢", YELLOW: "🟡", RED: "🔴" }[v];
  const tip = findings && findings.length
    ? `${v} — ${findings.length} finding(s):\n` + findings.map((f) => `• [${f.severity}] ${f.title}`).join("\n")
    : v === "GREEN" ? "GREEN — no safety issues found." : v;
  return <span className={`chip ${cls}`} title={tip}>{dot} {v}{findings && findings.length ? ` ·${findings.length}` : ""}</span>;
}

// eslint-disable-next-line react-refresh/only-export-components -- tooltip helper co-located with the chips it serves
export function authorTip(s: AuthorStats | null | undefined): string {
  if (!s || s.merged == null) return "Author stats unavailable.";
  const pct = (v: number | undefined): string => (v != null ? `${Math.round(v * 100)}%` : "—");
  return [
    `@${s.handle}`,
    `Adjusted merge rate: ${pct(s.merge_rate_shrunk)}  ·  Merge rate: ${pct(s.merge_rate)}`,
    `PRs: ${s.total} total · ${s.merged} merged · ${s.open} open · ${s.closed_unmerged} closed-unmerged`,
    `Comments: ${s.comments}`,
  ].join("\n");
}

const SEG = [
  { key: "GREEN", emoji: "🟢", cls: "chip-green", count: (r: SafetyRollup) => r.green },
  { key: "YELLOW", emoji: "🟡", cls: "chip-yellow", count: (r: SafetyRollup) => r.yellow },
  { key: "RED", emoji: "🔴", cls: "chip-red", count: (r: SafetyRollup) => r.red },
  { key: "UNKNOWN", emoji: "❔", cls: "chip-muted", count: (r: SafetyRollup) => r.unknown ?? 0 },
];

export function SafetyRollupChip({ r, prs = [], emptyTip }: { r: SafetyRollup; prs?: SafetyPR[]; emptyTip?: string }) {
  const [hover, setHover] = useState<{ key: string; x: number; y: number } | null>(null);
  if (!r || (r.green + r.yellow + r.red + (r.unknown ?? 0)) === 0)
    return <span className="chip chip-muted" title={emptyTip ?? "No PR routed to merge — nothing to security-audit."}>—</span>;

  const forKey = (key: string) => prs.filter((p) => (p.verdict || "").toUpperCase() === key);
  const staleCount = (key: string) => forKey(key).filter((p) => p.fresh === false).length;

  return (
    <span className="rollup" onMouseLeave={() => setHover(null)}>
      {SEG.filter((s) => s.count(r) > 0).map((s) => {
        const stale = staleCount(s.key);
        return (
          <span key={s.key}
            className={`chip ${s.cls} rollup-seg${stale > 0 ? " rollup-seg-stale" : ""}`}
            title={stale > 0 ? `${stale} of these verdict(s) are stale — the PR head moved or the review aged out, so the merge gate treats them as not run.` : undefined}
            onMouseEnter={(e) => { const b = (e.target as HTMLElement).getBoundingClientRect(); setHover({ key: s.key, x: b.left, y: b.bottom }); }}>
            {s.emoji}{s.count(r)}{stale > 0 ? " ⟳" : ""}
          </span>
        );
      })}
      {hover && (
        <div className="safety-pop" style={{ left: Math.min(hover.x, window.innerWidth - 360), top: hover.y + 4 }}>
          {(() => {
            const list = forKey(hover.key);
            if (list.length === 0) return <div className="muted small">No reviewed PRs at this level.</div>;
            return list.map((p) => (
              <div key={p.pr} className="safety-pop-pr">
                <div className="safety-pop-head"><b>#{p.pr}</b> <span className={`sev sev-${p.verdict?.toLowerCase()}`}>{p.verdict}</span>{" "}
                  {p.fresh === false && <><span className="chip chip-yellow sm" title="The PR head moved since this review ran, or the review aged past the security window — the merge gate treats it as not run.">stale ⟳</span>{" "}</>}
                  {p.gating
                    ? <span className="chip chip-red sm" title="This PR is routed to merge by the briefing — its verdict gates the cluster.">gates merge</span>
                    : <span className="chip chip-muted sm" title="Not routed to merge (being closed / folded / needs-human) — verdict is informational only.">info only</span>}{" "}
                  <span className="muted small">{p.title?.slice(0, 50)}</span></div>
                {(p.verdict || "").toUpperCase() === "UNKNOWN"
                  ? <div className="muted small">⚠ Not yet safety-reviewed — diagnose this cluster before merging.</div>
                  : p.findings.length === 0
                    ? <div className="muted small">No concerns flagged.</div>
                    : <ul className="safety-pop-findings">{p.findings.map((f, i) => <li key={i}>{f.title}</li>)}</ul>}
                {p.fresh === false &&
                  <div className="muted small">⟳ Stale verdict — re-run SECURITY to clear the merge gate.</div>}
              </div>
            ));
          })()}
        </div>
      )}
    </span>
  );
}

const TIER_TONE: Record<number, string> = {
  0: "chip-red", 1: "chip-yellow", 2: "chip-muted", 3: "chip-green",
};

/** Path-based blast-radius tier of the files a PR touches (T0 = core/supply
 *  chain … T3 = leaf). Unknown tier (no cached diff) renders nothing — the
 *  caller shows its own absence marker. `pinnedBy` lists the changed paths
 *  that set the tier. */
export function TierChip({ tier, pinnedBy }: { tier?: number | null; pinnedBy?: string[] }) {
  if (tier == null) return null;
  const shown = (pinnedBy ?? []).slice(0, 8);
  const why = shown.length
    ? { rationale: shown.join(", ") + ((pinnedBy?.length ?? 0) > 8 ? ` +${(pinnedBy?.length ?? 0) - 8} more` : ""),
        label: `Tier ${tier} paths` }
    : null;
  return (
    <InfoTip entry={term("col.tier")} why={why} cue={false} focusable={false}>
      <span className={`chip ${TIER_TONE[tier] ?? "chip-muted"} sm`}>T{tier}</span>
    </InfoTip>
  );
}

/** "Mark as seen" for a PR's community-response signal (#537). Once acked, this
 *  control renders a muted "✓ seen by {who}" in place of the actionable button;
 *  the row itself drops out of the `responses` filter, without reappearing
 *  until a newer response supersedes the acknowledgment. */
export function AckButton({ pr, ack, onAck }: { pr: number; ack: PRResponseAck | null; onAck: (pr: number) => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  if (ack) {
    return (
      <span className="chip chip-muted sm" title={`Marked as seen by ${ack.by}`}>
        ✓ seen by {ack.by}
      </span>
    );
  }
  return (
    <button
      className="chip chip-muted sm ack-btn"
      title="Mark as seen — hides it for every operator until a newer response arrives"
      disabled={busy}
      onClick={async (e) => {
        e.stopPropagation();
        setBusy(true);
        try { await onAck(pr); } finally { setBusy(false); }
      }}>
      {busy ? "…" : "✓ seen"}
    </button>
  );
}

export function DriftChip({ s }: { s: string | null }) {
  if (!s) return null;
  const cls =
    s === "applicable" ? "chip-blue" :
    s === "conflicts" ? "chip-yellow" :
    "chip-muted";
  return (
    <InfoTip entry={term(`drift.${s}`)} cue={false} focusable={false}>
      <span className={`chip ${cls}`}>{s}</span>
    </InfoTip>
  );
}
