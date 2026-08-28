import { useEffect, useState } from "react";
import { api, type ProbeFinding } from "./api";

export type AgentProvider = "claude" | "codex";
export type AgentPick = AgentProvider | "none" | null;

/** This machine's readiness for the provider selected in the form. */
export function useAgentProbe(provider: AgentProvider | null): ProbeFinding | undefined {
  const [result, setResult] = useState<{
    provider: AgentProvider;
    found: ProbeFinding | undefined;
  }>();
  useEffect(() => {
    if (provider == null) return;
    let stale = false;
    void api.onboardingProbe({ agent: true, agent_provider: provider })
      .then((r) => { if (!stale) setResult({ provider, found: r.agent }); })
      .catch(() => { if (!stale) setResult({ provider, found: undefined }); });
    return () => { stale = true; };
  }, [provider]);
  return result?.provider === provider ? result.found : undefined;
}
