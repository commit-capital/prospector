import { Link } from "react-router";

/** Where a missing summary card comes from: ANALYZE writes it, so the page
 *  points at the Control tab's analysis job. */
export function AnalyzeJobLink({ kind, label }: { kind: "analyze-clusters" | "issue-analyze"; label: string }) {
  return (
    <Link to={`/pipeline/control?job=${kind}`} className="small">
      Run {label} to write the summary →
    </Link>
  );
}
