import { Fragment, useEffect, useState } from "react";
import { Link } from "react-router";
import { api, type FixQueue } from "../../api";
import { useExec } from "../../ExecContext";
import { PRLink } from "../../components/PRLink";
import { SandboxChecks } from "../../components/SandboxChecks";
import { ago, elapsed, fixStatusChip, fmt } from "./format";

/** The parked-and-ready chip, worded by what the action actually produced. */
const PARKED_READY: Record<string, string> = {
  update: "✅ Branch update ready",
  rebase: "✅ Rebase ready",
  resolve: "✅ Conflicts resolved",
  fix: "✅ Fix drafted",
  describe: "✅ Description drafted",
};

/** For a running queue row: which machine holds the claim, and a warning when
 *  that machine's worker heartbeat has gone quiet — a claimed run whose worker
 *  stopped beating is stuck, not slow. */
function RunningMeta({ host, online }: { host?: string | null; online: boolean }) {
  if (!host) return null;
  if (online) return <div className="muted small">on {host}</div>;
  return (
    <div className="small queue-warn"
      title={`${host}'s worker has stopped heartbeating, so this run is not progressing. Restart the worker on ${host}, then re-queue if the row doesn't recover.`}>
      on {host} — worker offline?
    </div>
  );
}

// Rows the fix table shows before "show all".
const FIX_ROWS = 10;

export function FixTable({ queue, onChanged }: { queue: FixQueue; onChanged: () => Promise<unknown> }) {
  const { dryRun, pushToast, pushIdentity } = useExec();
  const [showAll, setShowAll] = useState(false);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState<number>(() => Date.now());
  const live = queue.queue.some((e) => e.status === "running" || e.status === "pushing");

  // Elapsed time on a running row ticks between polls, so the seconds count up
  // rather than jumping.
  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(t);
  }, [live]);

  /** Approve or discard one parked change. The push itself happens on the
   *  worker's next tick, so this reloads rather than reporting a landed push.
   *  In dry-run, an approve answers with what it would push and changes
   *  nothing. */
  const act = async (pr: number, kind: "approve" | "discard" | "escalate") => {
    setBusy(pr);
    setError(null);
    try {
      if (kind === "approve") {
        const res = await api.approveFix(pr, dryRun);
        if (res.status === "dry-run") {
          pushToast(`(dry run) #${pr} · ${res.action ?? "fix"} push`, "yellow", { detail: res.detail });
        }
      } else if (kind === "discard") {
        await api.dequeueFix(pr);
      } else {
        await api.queueFix(pr, "rebase");
      }
      await onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="queue-table">
      <div className="muted small" style={{ marginBottom: 8 }}>
        Proven in the sandbox, pushed to nobody. Approving re-runs the merge or rebase against current base
        before anything reaches the branch. A run that ends stays here for half an hour, then moves to Run
        history. Parked changes also wait under “Your move” on <Link to="/">Home</Link>.
        {queue.runner.objection_budget && (
          <> · continuations today {queue.runner.objection_budget.used}/{queue.runner.objection_budget.limit}</>
        )}
      </div>
      <table className="grid compact">
        <thead><tr><th>Status</th><th>PR</th><th>Action</th><th>Source</th><th>Since</th><th></th></tr></thead>
        <tbody>
          {queue.queue.length === 0 ? (
            <tr><td colSpan={6} className="muted small">Nothing queued, running, waiting for review, or finished recently.</td></tr>
          ) : (showAll ? queue.queue : queue.queue.slice(0, FIX_ROWS)).map((e) => {
            const running = e.status === "running" || e.status === "pushing";
            const ended = e.finished_at != null;
            const detail = running && e.action === "fix" && e.guidance ? `goal: “${e.guidance}”` : e.detail;
            return (
              <Fragment key={e.pr}>
                <tr>
                  <td>
                    {e.status === "awaiting-review" ? (
                      <>
                        <span className={e.resolvable ? "chip chip-green sm" : "chip chip-amber sm"}
                          title={e.resolvable
                            ? "The action produced a change and the compile preflight did not reject it."
                            : "The change is parked, but its compile preflight did not pass."}>
                          {e.resolvable ? PARKED_READY[e.action] ?? "✅ Ready" : "⚠ Needs a look"}
                        </span>
                        {e.auto_review && (
                          <span className={e.auto_review.ok ? "chip chip-green sm" : "chip chip-amber sm"}
                            title={e.auto_review.reason}>
                            🤖 {e.auto_review.ok ? "agents cleared" : "agents left it for you"}
                          </span>
                        )}
                        {e.rounds > 0 && (
                          <span className="chip chip-blue sm" title={e.objection?.text ?? undefined}>
                            🔁 continued after review ×{e.rounds}
                          </span>
                        )}
                      </>
                    ) : (
                      <>
                        <span className={fixStatusChip(e.status)}>{e.status}{e.step && running ? ` · ${e.step}` : ""}</span>
                        {running && (
                          <RunningMeta host={e.host}
                            online={(queue.runner.hosts ?? []).some((h) => h.host === e.host && h.online)} />
                        )}
                      </>
                    )}
                  </td>
                  <td className="mono">
                    <PRLink n={e.pr} />
                    {e.title && <div className="muted small">{e.title.slice(0, 60)}</div>}
                  </td>
                  <td><span className="chip chip-muted sm">{e.action}</span></td>
                  <td>
                    <span className="chip chip-muted sm" title={e.objection?.text ?? undefined}>
                      {e.source === "objection" ? "from objection" : e.source === "auto" ? "auto" : "manual"}
                    </span>
                  </td>
                  <td className="muted small" title={fmt(e.finished_at ?? e.started_at ?? e.queued_at)}>
                    {running ? elapsed(e.started_at, now) : ended ? ago(e.finished_at) : ago(e.started_at ?? e.queued_at)}
                  </td>
                  <td>
                    <div style={{ display: "flex", gap: 6 }}>
                      {e.status === "awaiting-review" && (
                        <>
                          <button className="btn-primary sm" disabled={busy != null}
                            onClick={() => act(e.pr, "approve")}
                            title={dryRun
                              ? "Dry run: preview what this would push — nothing reaches GitHub until you switch to Live."
                              : `Push this ${e.action} to PR #${e.pr} as ${pushIdentity?.login ?? "the machine user"}.`}>
                            {busy === e.pr ? "…" : dryRun ? "✓ Push (dry run)" : "✓ Push"}
                          </button>
                          <button className="btn-secondary sm" disabled={busy != null}
                            onClick={() => act(e.pr, "discard")}
                            title="Discard this proven change without pushing it.">
                            ✕
                          </button>
                        </>
                      )}
                      {e.status === "refused" && e.conflict_paths.length > 0 && (
                        <button className="btn-secondary sm" disabled={busy != null}
                          onClick={() => act(e.pr, "escalate")}
                          title={`Re-queue the rebase as you rather than the hunter. An operator-queued rebase that pauses on these conflicts hands them to the agent resolver, and the result parks here for your review. Conflicted: ${e.conflict_paths.join(", ")}`}>
                          {busy === e.pr ? "…" : "⤴ Escalate to agent resolve"}
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
                {(detail || e.checks.length > 0) && (
                  <tr>
                    <td />
                    <td colSpan={5} className="muted small" style={{ paddingTop: 0 }}>
                      {detail}
                      {detail && e.checks.length > 0 && " · "}
                      <SandboxChecks checks={e.checks} />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
      {queue.queue.length > FIX_ROWS && (
        <button className="linkish small" style={{ marginTop: 6 }} onClick={() => setShowAll((v) => !v)}>
          {showAll ? "show fewer ▴" : `show all ${queue.queue.length} ▾`}
        </button>
      )}
      {error && <div className="muted small" style={{ marginTop: 6 }}>{error}</div>}
    </div>
  );
}
