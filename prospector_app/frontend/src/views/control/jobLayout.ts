import type { SuggestedAction } from "../../api";

/** The PR pipeline's run order, by job kind: each step feeds the next. */
export const PIPELINE_STEPS: { kind: string; label: string }[] = [
  { kind: "ingest", label: "Ingest" },
  { kind: "threat-scan", label: "Threat scan" },
  { kind: "cluster-new", label: "Clustering" },
  { kind: "analyze-clusters", label: "Analysis" },
  { kind: "security-review", label: "Security review" },
];

/** A job's 1-based step in the PR pipeline, or null for a job outside it. */
export function pipelineStep(kind: string): number | null {
  const i = PIPELINE_STEPS.findIndex((s) => s.kind === kind);
  return i < 0 ? null : i + 1;
}

/** The Run a job panel's order: the jobs a suggestion recommends, in the
 *  suggestions' order and each once, then every other job as a tile. */
export function layoutJobs<S extends { kind: string }>(specs: S[], suggestions: SuggestedAction[]): {
  recommended: { spec: S; suggestion: SuggestedAction }[];
  rest: S[];
} {
  const recommended: { spec: S; suggestion: SuggestedAction }[] = [];
  for (const suggestion of suggestions) {
    const spec = specs.find((s) => s.kind === suggestion.kind);
    if (spec && !recommended.some((r) => r.spec.kind === spec.kind)) recommended.push({ spec, suggestion });
  }
  const picked = new Set(recommended.map((r) => r.spec.kind));
  return { recommended, rest: specs.filter((s) => !picked.has(s.kind)) };
}
