import type { ProbeFinding } from "../api";
import { useAgentProbe, type AgentPick } from "../agentProvider";

function AgentFinding({ found }: { found: ProbeFinding | undefined }) {
  if (!found) return null;
  return found.ok
    ? <span className="chip chip-green sm">looks good</span>
    : <span className="chip chip-red sm">{found.problem ?? "no"}</span>;
}

/** Who backs the “Ask the agent” sidebar on this machine. */
export function AgentProviderChooser({ pick, onPick, title }: {
  pick: AgentPick;
  onPick: (pick: Exclude<AgentPick, null>) => void;
  title?: string;
}) {
  const provider = pick === "claude" || pick === "codex" ? pick : null;
  const found = useAgentProbe(provider);
  return (
    <div className="welcome-fork">
      {title && <h4>{title}</h4>}
      <label className={pick === "claude" ? "fork-opt on" : "fork-opt"}>
        <input type="radio" checked={pick === "claude"} onChange={() => onPick("claude")} />
        {" "}<strong>Use your Claude account</strong>
        {" "}<span className="muted small">
          the “Ask the agent” sidebar runs the Claude Code CLI on this computer,
          under your own login.
        </span>
        {pick === "claude" && <>{" "}<AgentFinding found={found} /></>}
        {pick === "claude" && found && !found.ok && (
          <p className="muted small">
            The sidebar shows how to finish installing or logging in.
          </p>
        )}
      </label>
      <label className={pick === "codex" ? "fork-opt on" : "fork-opt"}>
        <input type="radio" checked={pick === "codex"} onChange={() => onPick("codex")} />
        {" "}<strong>Use your Codex account</strong>
        {" "}<span className="muted small">
          the “Ask the agent” sidebar runs the Codex CLI on this computer,
          under your own login.
        </span>
        {pick === "codex" && <>{" "}<AgentFinding found={found} /></>}
        {pick === "codex" && found && !found.ok && (
          <p className="muted small">
            The sidebar shows how to finish installing or logging in.
          </p>
        )}
      </label>
      <label className={pick === "none" ? "fork-opt on" : "fork-opt"}>
        <input type="radio" checked={pick === "none"} onChange={() => onPick("none")} />
        {" "}<strong>No agent support</strong>
        {" "}<span className="muted small">hides the sidebar.</span>
      </label>
    </div>
  );
}
