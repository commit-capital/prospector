import type { ChecksRollup } from "../api";

const ICON = { pass: "✓", warn: "⚠", fail: "✗", na: "–" } as const;

/** "8/10 checks" badge colored by ratio, with a per-check hover breakdown. */
export function ChecksChip({ c }: { c: ChecksRollup | undefined }) {
  if (!c || c.total === 0) return <span className="chip chip-muted sm" title="No checks available for this PR yet.">—</span>;
  const ratio = c.passed / c.total;
  const cls = ratio === 1 ? "chip-green" : ratio >= 0.6 ? "chip-amber" : "chip-red";
  const failing = c.checks.filter((x) => x.status === "fail").length;
  const warns = c.checks.filter((x) => x.status === "warn").length;
  const tip = [
    `${c.passed}/${c.total} checks passed` + (failing ? ` · ${failing} failing` : "") + (warns ? ` · ${warns} warning` : ""),
    "",
    ...c.checks.map((x) => `${ICON[x.status]} ${x.name}${x.detail ? ` — ${x.detail}` : ""}`),
  ].join("\n");
  return <span className={`chip ${cls} sm checks-chip`} title={tip}>{c.passed}/{c.total} ✓</span>;
}
