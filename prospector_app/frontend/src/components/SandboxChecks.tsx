import { Fragment } from "react";

import type { SandboxCheck } from "../api";

function outcome(c: SandboxCheck): { mark: string; note: string | null; cls: string } {
  if (c.error_kind || c.error) {
    return { mark: "✗", note: c.error_kind ?? "error", cls: "chk-error" };
  }
  if (c.exit === 0) return { mark: "✓", note: null, cls: "chk-pass" };
  return { mark: "✗", note: null, cls: "chk-fail" };
}

function tooltip(c: SandboxCheck): string {
  const lines: string[] = [c.cmd ?? c.kind];
  if (c.files.length > 0) lines.push(`files: ${c.files.join(", ")}`);
  lines.push(c.exit == null
    ? "the command did not run"
    : `exit ${c.exit}${c.duration_s != null ? ` after ${c.duration_s}s` : ""}`);
  if (c.error) lines.push(c.error);
  if (c.error_excerpt) lines.push(c.error_excerpt);
  return lines.join("\n");
}

/** The sandbox runs an authoring agent made, one line: the lane, whether it
 *  passed, and why it could not run when it did not. Each run's command and
 *  error text are on hover. Renders nothing when the agent ran no check. */
export function SandboxChecks({ checks }: { checks: SandboxCheck[] | null | undefined }) {
  if (!checks || checks.length === 0) return null;
  return (
    <span className="sandbox-checks">
      checks:{" "}
      {checks.map((c, i) => {
        const o = outcome(c);
        return (
          <Fragment key={i}>
            {i > 0 && " · "}
            <span className={o.cls} title={tooltip(c)}>
              {c.kind} {o.mark}{o.note ? ` (${o.note})` : ""}
            </span>
          </Fragment>
        );
      })}
    </span>
  );
}
