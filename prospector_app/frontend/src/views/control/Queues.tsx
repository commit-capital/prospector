import { Fragment, useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { api, type Autohunt, type FixQueue, type VerifyQueue } from "../../api";
import { useExec } from "../../ExecContext";
import { PRLink } from "../../components/PRLink";
import { SandboxChecks } from "../../components/SandboxChecks";
import { Panel } from "./Panel";
import { ago, elapsed, fixStatusChip, fmt, queueStatusChip } from "./format";

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

function VerifyTable({ queue, hunt }: { queue: VerifyQueue; hunt: Autohunt | null }) {
  return (
    <table className="grid compact queue-table">
      <thead><tr><th>Status</th><th>PR</th><th>Source</th><th>Since</th></tr></thead>
      <tbody>
        {queue.queue.length === 0 ? (
          <tr><td colSpan={4} className="muted small">Nothing queued, waiting, or running.</td></tr>
        ) : queue.queue.map((e) => (
          <tr key={e.pr}>
            <td>
              <span className={queueStatusChip(e.status)}>{e.status}{e.step ? ` · ${e.step}` : ""}</span>
              {e.status === "running" && (
                <RunningMeta host={e.host}
                  online={(hunt?.status.runner.hosts ?? []).some((h) => h.host === e.host && h.online)} />
              )}
            </td>
            <td className="mono">
              <PRLink n={e.pr} />
              {e.title && <div className="muted small">{e.title.slice(0, 60)}</div>}
            </td>
            <td><span className="chip chip-muted sm">{e.source === "auto-resweep" ? "re-sweep" : e.source === "auto" ? "auto" : "manual"}</span></td>
            <td className="muted small" title={fmt(e.started_at ?? e.queued_at)}>{ago(e.started_at ?? e.queued_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function FixTable({ queue, onChanged }: { queue: FixQueue; onChanged: () => Promise<unknown> }) {
  const { dryRun, pushToast, pushIdentity } = useExec();
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
          ) : queue.queue.map((e) => {
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
      {error && <div className="muted small" style={{ marginTop: 6 }}>{error}</div>}
    </div>
  );
}

function plural(n: number, one: string, many: string = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** The three worker queues as cards; Verify and Fix open their tables. The
 *  Fix table opens by itself when a change is waiting for review. */
export function Queues({ hunt, verifyQueue, fixQueue, onFixChanged }: {
  hunt: Autohunt | null;
  verifyQueue: VerifyQueue | null;
  fixQueue: FixQueue | null;
  onFixChanged: () => Promise<unknown>;
}) {
  const [open, setOpen] = useState<"verify" | "fix" | null>(null);
  const opened = useRef(false);
  const awaiting = (fixQueue?.queue ?? []).filter((e) => e.status === "awaiting-review").length;
  useEffect(() => {
    if (opened.current || fixQueue == null) return;
    opened.current = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- open once, when the first fix queue read shows a parked change
    if (awaiting > 0) setOpen("fix");
  }, [fixQueue, awaiting]);

  const vq = verifyQueue?.queue ?? [];
  const count = (status: string) => vq.filter((e) => e.status === status).length;
  const fq = fixQueue?.queue ?? [];
  const fixRunning = fq.filter((e) => e.status === "running" || e.status === "pushing").length;
  const fixQueued = fq.filter((e) => e.status === "queued").length;
  const fixEnded = fq.filter((e) => e.finished_at != null).length;
  const toggle = (which: "verify" | "fix") => setOpen((o) => (o === which ? null : which));
  const join = (parts: (string | false)[]) => parts.filter(Boolean).join(" · ");

  return (
    <Panel id="queues" title="Queues">
      <div className="queue-cards">
        <div className="queue-card">
          <div className="queue-card-title">Security</div>
          <div className="queue-card-body">
            {hunt ? `${hunt.status.security_pool.toLocaleString()} awaiting review` : "loading…"}
          </div>
        </div>
        <button className={`queue-card${open === "verify" ? " open" : ""}`} aria-expanded={open === "verify"}
          onClick={() => toggle("verify")}>
          <div className="queue-card-title">Verify {open === "verify" ? "▴" : "▾"}</div>
          <div className="queue-card-body">
            {verifyQueue
              ? join([`${count("running")} running`, `${count("queued")} queued`,
                  count("waiting-for-base") > 0 && `${count("waiting-for-base")} waiting for base`,
                  hunt != null && `${hunt.status.verify_pool.toLocaleString()} GREEN awaiting verify`])
              : "loading…"}
          </div>
        </button>
        <button className={`queue-card${open === "fix" ? " open" : ""}${awaiting > 0 ? " needs-you" : ""}`}
          aria-expanded={open === "fix"} onClick={() => toggle("fix")}>
          <div className="queue-card-title">Fix {open === "fix" ? "▴" : "▾"}</div>
          <div className="queue-card-body">
            {fixQueue
              ? <>{join([`${fixRunning} running`, fixQueued > 0 && `${fixQueued} queued`,
                  fixEnded > 0 && `${fixEnded} ended recently`])}
                {awaiting > 0 && <> · <b>{plural(awaiting, "change")} awaiting you</b></>}</>
              : "loading…"}
          </div>
        </button>
      </div>
      {open === "verify" && verifyQueue && <VerifyTable queue={verifyQueue} hunt={hunt} />}
      {open === "fix" && fixQueue && <FixTable queue={fixQueue} onChanged={onFixChanged} />}
    </Panel>
  );
}
