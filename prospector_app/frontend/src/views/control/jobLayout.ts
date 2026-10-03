import type { SuggestedAction } from "../../api";

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
