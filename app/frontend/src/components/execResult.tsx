// The minimal shape both PR (ExecResult) and issue (IssueExecResult) results share.
type ExecLike = { status: string; detail: string };

// A landed exec result — the write actually happened (not dry-run/blocked/error).
// eslint-disable-next-line react-refresh/only-export-components -- predicate co-located with the chip it serves
export const landed = (r: ExecLike | null): boolean =>
  r?.status === "executed" || r?.status === "reopened" || r?.status === "merged";

// The status chip the action bars render after firing.
export function ExecResultChip({ result }: { result: ExecLike }) {
  const cls = landed(result) ? "green" : result.status === "error" || result.status === "blocked" ? "red" : "muted";
  return <span className={`chip chip-${cls}`} title={result.detail}>{result.status}</span>;
}
