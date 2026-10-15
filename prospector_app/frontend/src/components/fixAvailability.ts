import type { FixRequest, FixRunner, ReviewFinding } from "../api";

/** Why an action cannot be queued right now, or null when it can be.
 *  Queueing is gated on a worker being reachable, NOT on this browser's backend
 *  holding the push key — the queue is shared so an operator clicks from a
 *  laptop and the machine with the key does the work. Eligibility itself is the
 *  backend's call (gates.fix_eligibility); it reports its reason on the click
 *  rather than being second-guessed here. */
export function unavailable(runner: FixRunner | null, req: FixRequest | null,
                            resolved: boolean): string | null {
  if (resolved) return "This PR is already resolved.";
  if (runner != null && !runner.can_queue) {
    return "No autofix worker has been seen against this store, so a queued action "
         + "would never run. Start a backend with TRIAGE_FIX_WORKER=1 on the machine "
         + "holding the machine user's push key.";
  }
  const status = req?.status;
  if (status && IN_FLIGHT.includes(status)) {
    return `PR already has a ${status} ${req?.action} request.`;
  }
  return null;
}

const IN_FLIGHT = ["queued", "running", "awaiting-review", "approved", "pushing"];

/** An autofix goal naming each design review finding: every open one its
 *  verifier did not refute. */
export function designFixGoal(findings: ReviewFinding[]): string {
  const lines = findings
    .filter((f) => !f.resolved && !f.outdated && f.verification?.verdict !== "refuted")
    .map((f) => {
      const where = f.path ? `${f.path}${f.line != null ? `:${f.line}` : ""} ` : "";
      return `- ${where}(${f.severity ?? "minor"}) ${f.title ?? f.body.split("\n")[0]}`;
    });
  return lines.length === 0 ? ""
    : `Fix these design review findings, and nothing else:\n${lines.join("\n")}`;
}
