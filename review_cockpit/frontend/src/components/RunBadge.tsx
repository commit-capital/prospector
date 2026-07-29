import type { RunState } from "../api";

const KIND_LABEL: Record<string, string> = { close: "closed", merge: "merged", reopen: "reopened" };

function ago(at?: string): string {
  if (!at) return "";
  const t = at.replace("T", " ").slice(5, 16); // MM-DD HH:MM
  return t;
}

/** "✓ closed · CLOSE_DUP · 06-10 10:00" — shows that a live action already
 *  landed on this PR (#10). Green when done, muted when reopened/undone. */
export function RunBadge({ rs, compact = false }: { rs?: RunState; compact?: boolean }) {
  if (!rs) return null;
  const label = KIND_LABEL[rs.kind] ?? rs.kind;
  const tone = rs.done ? "green" : "muted";
  const mark = rs.done ? "✓" : "↩";
  const title = `${rs.done ? "Already" : "Last action:"} ${label}${rs.reason ? ` (${rs.reason})` : ""} at ${rs.at}` +
    (rs.undoable ? " · reopenable" : rs.kind === "merge" ? " · permanent" : "");
  return (
    <span className={`chip chip-${tone} sm run-badge`} title={title}>
      {mark} {label}{!compact && rs.reason ? ` · ${rs.reason}` : ""}{!compact && ` · ${ago(rs.at)}`}
    </span>
  );
}
