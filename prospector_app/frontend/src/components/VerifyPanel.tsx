import type { VerifyDetail, VerifyFault, VerifyLevel, VerifyReasonMatch, VerifyRequest, VerifyRunner, VerifySignals, VerifyStep } from "../api";
import type { ReactNode } from "react";
import { Collapsible } from "./Collapsible";
import { localDateTime, timeAgo } from "../timeAgo";
import { useRepoMeta } from "../RepoMetaContext";

/** The operator's #1 question when verification isn't green: whose fault is it?
 *  A leading badge answers it before any detail — PR-fault and harness-fault
 *  must never look alike. */
// The store's verify error kinds, in the operator's words; the raw kind stays
// in the chip's title.
const ERROR_KIND_LABEL: Record<string, string> = {
  "base-lane": "default branch fails its own command",
  "base-compile": "default branch fails its own command",
  "sandbox-error": "sandbox could not run",
  "agent-unavailable": "agent CLI offline",
  "agent-failed": "agent run failed",
  "fetch-error": "GitHub fetch failed",
  "no-base": "no base branch pinned",
  "interrupted": "worker restarted mid-run",
  "exception": "worker crashed",
  "hold": "held by the worker",
  "refused-safety": "refused by the safety check",
};

function FaultBadge({ fault }: { fault: VerifyFault }) {
  if (!fault) return null;
  const spec: Record<"pr" | "system" | "judgment", { cls: string; text: string; title: string }> = {
    pr: { cls: "chip-red", text: "the PR's fault", title: "Verification failed because of the pull request itself — its test, its rebase state, or a regression it introduces. The author needs to act." },
    system: { cls: "chip-yellow", text: "harness fault — not the PR", title: "Verification could not run or complete because of the harness or infrastructure (an errored run, a hold, an unhealthy sandbox). This says nothing about the PR; re-queue once the harness is fixed." },
    judgment: { cls: "chip-purple", text: "needs your call", title: "The signals disagree or the reproduction was flaky — the phase can't resolve it. A human must read the evidence and decide." },
  };
  const s = spec[fault];
  return <span className={`chip ${s.cls} sm`} style={{ marginLeft: 8 }} title={s.title}>{s.text}</span>;
}

/** Merge-gate lane results (compile/build): host-observed exits over the
 *  patched tree. Green = passed, red = failed, muted = skipped. */
function LaneChips({ lanes }: { lanes: NonNullable<VerifySignals["lanes"]> | undefined }) {
  if (!lanes || Object.keys(lanes).length === 0) return null;
  return (
    <span style={{ marginLeft: 8 }}>
      {Object.entries(lanes).map(([name, e]) => (
        <span
          key={name}
          className={`chip sm ${e.exit === 0 ? "chip-green" : e.skipped ? "chip-muted" : "chip-red"}`}
          title={e.cmd ? `${e.cmd}${e.duration_s != null ? ` — ${e.duration_s}s` : ""}${e.error_excerpt ? ` — ${e.error_excerpt}` : ""}${e.skipped ? ` — skipped: ${e.skipped}` : ""}` : name}
        >
          {name} {e.exit === 0 ? "✓" : e.skipped ? "–" : "✗"}
        </span>
      ))}
    </span>
  );
}

const LEVEL_TONE: Record<VerifyLevel, string> = {
  verified: "v-safe", attention: "v-caution", blocked: "v-risk", info: "v-unknown", pending: "v-unknown",
};
const LEVEL_ICON: Record<VerifyLevel, string> = {
  verified: "✅", attention: "🚨", blocked: "⛔", info: "ℹ️", pending: "⏳",
};

/** Per-phase decode of the trusted sentinel exits (sandbox/run-phase.sh): the
 *  host accepts a signal only on an exact match, so any other exit is an
 *  infrastructure error, never a verdict. The same code means different things
 *  per phase — exit 0 on the red run is a failure of the check, not a pass. */
const APPLY_DECODE: Record<number, string> = {
  0: "applied cleanly", 10: "boot probe failed", 20: "test failed", 30: "patch conflict — needs rebase",
};
const RED_DECODE: Record<number, string> = {
  0: "did NOT fail — bug not reproduced", 10: "boot probe failed", 20: "failed, as required here", 30: "patch conflict",
};
const GREEN_DECODE: Record<number, string> = {
  0: "passed, as required here", 10: "boot probe failed", 20: "STILL failing with the fix", 30: "patch conflict",
};

const EXIT_TRUST = "Exit codes the trusted host observed from each phase container. Untrusted code cannot forge its own PID-1 exit, so these are the phase's facts; a red is accepted only on exactly 20.";

/** A host-observed exit code on its step's line. The decode shows inline only
 *  when the code is not the one the phase requires. */
function ExitChip({ label, code, expected, decode }: {
  label?: string; code: number | null | undefined; expected: number; decode: Record<number, string>;
}) {
  if (code == null) return null;
  const decoded = decode[code] ?? "infrastructure error — never a signal";
  const ok = code === expected;
  return (
    <span className={`verify-exit ${ok ? "ok" : "bad"}`} title={`${EXIT_TRUST} ${code} = ${decoded}.`}>
      🔒 {label && <>{label} </>}exit <b>{code}</b>{!ok && <span className="muted"> {decoded}</span>}
    </span>
  );
}

const STEP_ICON: Record<VerifyStep["result"], string> = {
  pass: "✓", fail: "✗", warn: "⚠", skip: "–", info: "ℹ",
};

/** What a step carries beside its server-written line: trusted exit chips,
 *  trust tags, and the evidence its one "details" toggle opens on. */
interface StepExtra {
  chips: ReactNode[];
  tags: ReactNode[];
  details: ReactNode[];
  reasonings: { label: string; text: string }[];
  open: boolean;
}

const emptyExtra = (): StepExtra => ({ chips: [], tags: [], details: [], reasonings: [], open: false });

function Field({ label, children }: { label: ReactNode; children: ReactNode }) {
  return <div className="verify-field"><span className="verify-field-label">{label}</span>{children}</div>;
}

function Reasoning({ label, text }: { label: string; text: string }) {
  return <Field label={label}><span className="verify-reasoning">{text}</span></Field>;
}

/** Hangs each signal's evidence on the step it belongs to. Each part names its
 *  step keys nearest-first; a part whose steps a run never reached lands on
 *  the story's last step, and with no story at all, in `orphan`. */
function stepExtras(v: VerifyDetail, steps: VerifyStep[]): { byKey: Map<string, StepExtra>; orphan: StepExtra } {
  const byKey = new Map<string, StepExtra>(steps.map((st) => [st.key, emptyExtra()]));
  const orphan = emptyExtra();
  const last = steps.length ? steps[steps.length - 1].key : null;
  const put = (keys: string[], part: Partial<StepExtra>) => {
    const key = keys.find((k) => byKey.has(k)) ?? last;
    const into = key ? byKey.get(key)! : orphan;
    into.chips.push(...(part.chips ?? []));
    into.tags.push(...(part.tags ?? []));
    into.details.push(...(part.details ?? []));
    into.reasonings.push(...(part.reasonings ?? []));
    into.open ||= part.open ?? false;
  };
  const reason = (label: string, text: string | null | undefined) => (text ? [{ label, text }] : []);

  const s = v.signals ?? {};
  const blind = s.blind_adequacy;
  const rg = s.red_green;
  const repro = s.independent_repro;
  const authored = s.authored_test;
  // an escalation exists to be read — open the judgment content by default
  const attention = v.level === "attention";

  if (blind) {
    put(["blind"], {
      tags: [
        blind.confidence && <span key="conf" className="muted small">· {blind.confidence} confidence</span>,
        <span key="pre" className="chip chip-blue sm" title="This judgment was committed to the store before any container ran — the phase's integrity property. It cannot have been rationalized backward from a green result.">committed before any run</span>,
      ],
      details: [
        blind.claimed_symptom && <Field key="sym" label="Claimed symptom">{blind.claimed_symptom}</Field>,
        blind.test_cmd && <Field key="cmd" label="Test command"><code className="verify-cmd">{blind.test_cmd}</code></Field>,
        (blind.requires_live_agent || blind.from_linked_issue || blind.has_test === false) && (
          <div key="flags" className="verify-field muted small">
            {blind.has_test === false && <>ships no test · </>}
            {blind.requires_live_agent && <>reproducing needs a live agent (Tier 2) · </>}
            {blind.from_linked_issue && <>repro taken from the linked issue</>}
          </div>
        ),
      ],
      reasonings: reason("Reasoning", blind.reasoning),
      open: attention,
    });
  }

  if (rg) {
    put(["apply"], { chips: [<ExitChip key="apply" code={rg.apply_exit} expected={0} decode={APPLY_DECODE} />] });
    put(["red"], { chips: [<ExitChip key="red" code={rg.red_exit} expected={20} decode={RED_DECODE} />] });
    put(["green"], { chips: [<ExitChip key="green" code={rg.green_exit} expected={0} decode={GREEN_DECODE} />] });
    if (byKey.has("confirm")) {
      put(["confirm"], { chips: [
        <ExitChip key="red2" label="red" code={rg.red_exit_confirm} expected={20} decode={RED_DECODE} />,
        <ExitChip key="green2" label="green" code={rg.green_exit_confirm} expected={0} decode={GREEN_DECODE} />,
      ] });
    }
  }

  // The comparison the phase turns on: the pre-committed prediction vs. the
  // red output that actually happened.
  if (blind?.expected_red_signature || rg?.red_output_tail) {
    const m = matchSummary(s.red_reason_match);
    put(["red", "author-red", "apply", "blind"], {
      details: [
        <Field key="match" label="Did the test fail the way the bug predicts?">{m.icon} {m.text}</Field>,
        <div key="cmp" className="verify-compare">
          <div>
            <div className="verify-block-label">📌 predicted red signature <span className="chip chip-blue sm" title="The blind pass's prediction of what the red output should look like, recorded before any result existed.">pre-committed</span></div>
            <Tail text={blind?.expected_red_signature} />
          </div>
          <div>
            <div className="verify-block-label">captured red output <UntrustedTag /></div>
            <Tail text={rg?.red_output_tail} />
          </div>
        </div>,
      ],
      reasonings: reason("Judge's reasoning", s.red_reason_match?.reasoning),
      open: attention,
    });
  }
  if (rg?.green_output_tail) {
    put(["green", "red", "apply", "blind"], {
      details: [<div key="green" className="verify-field"><div className="verify-block-label">test output with the fix <UntrustedTag /></div><Tail text={rg.green_output_tail} /></div>],
    });
  }

  // Corroborating evidence, never a gate. A repro the story has no step for
  // and that never ran has nothing to show.
  if (repro && (byKey.has("repro") || repro.ran || repro.output_tail)) {
    const m = matchSummary(s.repro_reason_match);
    put(["repro"], {
      chips: repro.ran ? [<ExitChip key="repro" code={repro.exit_code} expected={20} decode={RED_DECODE} />] : [],
      tags: [<span key="corr" className="chip chip-muted sm" title="The agent's own reproduction, run against the pinned default branch. Corroborating evidence only — it never changes the outcome.">corroborating, not a gate</span>],
      details: [
        repro.ran && <Field key="match" label="Did it fail the way the bug predicts?">{m.icon} {m.text}</Field>,
        blind?.expected_repro_signature && <Field key="sig" label="Predicted repro signature"><span className="verify-reasoning">{blind.expected_repro_signature}</span></Field>,
        blind?.repro_command && <Field key="cmd" label={<>Repro command <span className="muted small">(agent-authored)</span></>}><code className="verify-cmd">{blind.repro_command}</code></Field>,
        repro.output_tail && <div key="out" className="verify-field"><div className="verify-block-label">repro output <UntrustedTag /></div><Tail text={repro.output_tail} /></div>,
        repro.from_linked_issue && <div key="issue" className="muted small">Repro steps taken from the linked issue.</div>,
      ],
      reasonings: reason("Judge's reasoning", s.repro_reason_match?.reasoning),
    });
  }

  // The agent-authored test lane (no-test PRs) — corroborating evidence only.
  if (authored?.attempted) {
    const verified = v.outcome === "agent-verified";
    put(["author", "blind"], {
      tags: [
        <span key="corr" className="chip chip-muted sm" title="The PR ships no test, so the harness authored one. The agent wrote file contents only; the driver validated the paths (new test files only), built the patch, and derived the command itself. Corroborating evidence — it never counts toward the auto-merge bar.">corroborating, not a gate</span>,
        <span key="pre" className="chip chip-blue sm" title="The authored files and the predicted failure were committed to the store before any container ran — they cannot have been rationalized backward from a result.">committed before any run</span>,
      ],
      details: [
        authored.test_cmd && <Field key="cmd" label={<>Derived command <span className="muted small">(driver-derived from the authored paths)</span></>}><code className="verify-cmd">{authored.test_cmd}</code></Field>,
        authored.expected_red_signature && <Field key="sig" label="Predicted red signature"><span className="verify-reasoning">{authored.expected_red_signature}</span></Field>,
        ...(authored.files ?? []).map((f) => (
          <Collapsible key={`file:${f.path}`} summary={<>authored file <code>{f.path}</code></>}>
            <pre className="verify-tail">{f.contents}</pre>
          </Collapsible>
        )),
      ],
      reasonings: reason("Authoring reasoning", authored.reasoning),
      open: verified,
    });
    put(["author-red", "author", "blind"], {
      chips: [<ExitChip key="ared" code={authored.red_exit} expected={20} decode={RED_DECODE} />],
      details: [authored.red_output_tail && <div key="out" className="verify-field"><div className="verify-block-label">authored red output <UntrustedTag /></div><Tail text={authored.red_output_tail} /></div>],
    });
    put(["author-green", "author-red", "author", "blind"], {
      chips: [<ExitChip key="agreen" code={authored.green_exit} expected={0} decode={GREEN_DECODE} />],
      details: [authored.green_output_tail && <div key="out" className="verify-field"><div className="verify-block-label">authored green output <UntrustedTag /></div><Tail text={authored.green_output_tail} /></div>],
    });
    put(["author-confirm", "author-green", "author-red", "author", "blind"], {
      chips: [
        <ExitChip key="ared2" label="confirm red" code={authored.red_exit_confirm} expected={20} decode={RED_DECODE} />,
        <ExitChip key="agreen2" label="confirm green" code={authored.green_exit_confirm} expected={0} decode={GREEN_DECODE} />,
      ],
    });
  }
  return { byKey, orphan };
}

/** The reasonings and evidence behind one step; the step's own reasoning
 *  leads, and a signal repeating it is shown once. */
function ExtraDetails({ extra, reasoning, reasoningLabel }: { extra: StepExtra; reasoning: string | null; reasoningLabel: string }) {
  return (
    <>
      {reasoning && <Reasoning label={reasoningLabel} text={reasoning} />}
      {extra.reasonings.filter((r) => r.text !== reasoning).map((r) => <Reasoning key={r.label} {...r} />)}
      {extra.details}
    </>
  );
}

const hasDetails = (extra: StepExtra, reasoning: string | null): boolean =>
  !!reasoning || extra.reasonings.length > 0 || extra.details.some(Boolean);

/** The run's checks in order, in plain English — the first thing to read.
 *  Each step carries its trusted exit codes inline and one "details" toggle
 *  over its judgments and evidence; the step that broke opens it. */
function Story({ v }: { v: VerifyDetail }) {
  const steps = v.story ?? [];
  const { byKey, orphan } = stepExtras(v, steps);
  return (
    <>
      {steps.length > 0 && (
        <>
          <div className="verify-section-head">Steps:</div>
          <div className="verify-story">
            {steps.map((st) => {
              const extra = byKey.get(st.key) ?? emptyExtra();
              const label = st.key === "author" ? "Authoring reasoning" : "Judge's reasoning";
              return (
                <div key={st.key} className={`vstep vstep-${st.result}`}>
                  <span className="vstep-icon">{STEP_ICON[st.result] ?? "·"}</span>
                  <div className="vstep-body">
                    <div className="vstep-head">
                      <span className="vstep-label">{st.label}</span>
                      {extra.tags}
                      {extra.chips}
                    </div>
                    <div className="vstep-note">{st.note}</div>
                    {hasDetails(extra, st.reasoning) && (
                      <Collapsible defaultOpen={st.result === "fail" || extra.open} summary={<>details</>}>
                        <ExtraDetails extra={extra} reasoning={st.reasoning} reasoningLabel={label} />
                      </Collapsible>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}
      {(orphan.chips.some(Boolean) || hasDetails(orphan, null)) && (
        <Collapsible defaultOpen={orphan.open} summary={<>Evidence {orphan.tags}{orphan.chips}</>}>
          <ExtraDetails extra={orphan} reasoning={null} reasoningLabel="" />
        </Collapsible>
      )}
    </>
  );
}

/** "untrusted test output" label — these tails are the PR's own captured
 *  output; the driver treats them as advisory evidence, never a verdict. */
function UntrustedTag() {
  return (
    <span className="chip chip-yellow sm" title="This is the PR's own captured test output (capped at 8 KiB) — untrusted, attacker-influenced text. It is advisory evidence for the judge; no verdict derives from it.">
      ⚠ untrusted test output
    </span>
  );
}

function Tail({ text }: { text: string | null | undefined }) {
  if (!text) return <div className="muted small">No output captured.</div>;
  return <pre className="verify-tail">{text}</pre>;
}

function matchSummary(m: VerifyReasonMatch | undefined): { icon: string; text: string; tone: "green" | "red" | "muted" } {
  if (!m) return { icon: "–", text: "not judged", tone: "muted" };
  if (m.applicable === false) return { icon: "–", text: "n/a — no repro ran", tone: "muted" };
  if (m.matches) return { icon: "✓", text: `yes, it matches the prediction · ${m.confidence ?? "?"} confidence`, tone: "green" };
  return { icon: "✗", text: `NO — it does not match the prediction · ${m.confidence ?? "?"} confidence`, tone: "red" };
}

/** The queue/run state strip: where an in-flight or failed verification
 *  request stands. A `done` request renders nothing — the verdict banner
 *  below it is the result. */
function RequestStrip({ req, runner }: { req: VerifyRequest; runner: VerifyRunner | null }) {
  const offline = runner != null && !runner.online;
  const source = req.source === "auto-resweep"
    ? " · automatic re-sweep"
    : req.source === "auto" ? " · auto-picked" : "";
  if (req.status === "queued") {
    return (
      <div className="verdict-banner v-unknown">
        <span className="vb-icon">⏳</span>
        <div>
          <div className="vb-headline">
            Queued for sandbox verification
            {offline && <span className="chip chip-yellow sm" style={{ marginLeft: 8 }} title="No verification worker has beat recently, so the queue is not draining. Start the app backend with TRIAGE_VERIFY_WORKER=1 on the sandbox machine.">⚠ runner offline</span>}
          </div>
          <div className="vb-detail">
            Waiting for the runner{source}
            {req.queued_at && <span title={localDateTime(req.queued_at)}> · queued {timeAgo(req.queued_at)}</span>}
          </div>
        </div>
      </div>
    );
  }
  if (req.status === "waiting-for-base") {
    return (
      <div className="verdict-banner v-caution">
        <span className="vb-icon">⌛</span>
        <div>
          <div className="vb-headline">
            Waiting for a sandbox base
            {offline && <span className="chip chip-yellow sm" style={{ marginLeft: 8 }} title="No verification worker has beat recently, so the queue is not draining. Start the app backend with TRIAGE_VERIFY_WORKER=1 on the sandbox machine.">⚠ runner offline</span>}
          </div>
          <div className="vb-detail">
            {req.error ?? "The runner has no usable pinned base yet"}. The worker
            retries automatically once a prepare-base run lands (the wait is
            bounded, then this errors)
            {req.queued_at && <span title={localDateTime(req.queued_at)}> · queued {timeAgo(req.queued_at)}</span>}
            {source}
          </div>
        </div>
      </div>
    );
  }
  if (req.status === "running") {
    return (
      <div className="verdict-banner v-unknown">
        <span className="vb-icon">🔄</span>
        <div>
          <div className="vb-headline">Verification running{req.step ? ` — ${req.step}` : ""}</div>
          <div className="vb-detail">
            {req.started_at && <span title={localDateTime(req.started_at)}>started {timeAgo(req.started_at)} · </span>}
            blind verdict → sandbox red/green → judge; a full run can take many minutes.
            {source}
          </div>
        </div>
      </div>
    );
  }
  if (req.status === "error") {
    return (
      <div className="verdict-banner v-risk">
        <span className="vb-icon">💥</span>
        <div style={{ minWidth: 0 }}>
          <div className="vb-headline">
            Verification run failed
            <FaultBadge fault={req.fault ?? null} />
            {req.error_kind && (
              <span className="chip chip-muted sm" style={{ marginLeft: 8 }} title={`error kind: ${req.error_kind}`}>
                {ERROR_KIND_LABEL[req.error_kind] ?? req.error_kind}
              </span>
            )}
          </div>
          <div className="vb-detail">
            {req.error}
            {req.finished_at && <span title={localDateTime(req.finished_at)}> · {timeAgo(req.finished_at)}</span>}
          </div>
          {req.log_tail && (
            <Collapsible summary={<>run output tail</>}>
              <pre className="verify-tail">{req.log_tail}</pre>
            </Collapsible>
          )}
        </div>
      </div>
    );
  }
  if (req.status === "cancelled") {
    return (
      <div className="muted small">
        Verification request cancelled{req.finished_at && <span title={localDateTime(req.finished_at)}> · {timeAgo(req.finished_at)}</span>}.
      </div>
    );
  }
  return null;
}

/** The queue/re-verify/cancel control for the "Test run" check row
 *  (#581) — any open PR can be queued for the sandbox runner. `busy` names the
 *  in-flight action so the label reads "Queuing…"/"Cancelling…" rather than a
 *  bare ellipsis, and stays disabled-with-that-label until the caller's
 *  re-fetched PR state lands — never the pre-click button (#652). */
export function VerifyAction({ v, req, busy, canQueue, onQueue, onDequeue }: {
  v: VerifyDetail | null;
  req: VerifyRequest | null;
  busy: "queue" | "dequeue" | null;
  canQueue: boolean;
  onQueue: () => void;
  onDequeue: () => void;
}) {
  const branch = useRepoMeta().meta?.default_branch ?? "the default branch";
  const status = req?.status;
  if (!canQueue) return null;
  if (status === "running") {
    return <span className="chip chip-blue sm" title="A sandbox run is in flight for this PR.">verifying…</span>;
  }
  if (status === "queued" || status === "waiting-for-base") {
    return (
      <button className="btn-secondary sm" disabled={busy != null} onClick={onDequeue}
        title="Remove this PR from the verification queue before the runner picks it up.">
        {busy === "dequeue" ? "Cancelling…" : "✕ Cancel queued verification"}
      </button>
    );
  }
  return (
    <button className="btn-secondary sm" disabled={busy != null} onClick={onQueue}
      title={`Queue this PR for sandbox verification: the runner reproduces the claimed bug on pinned ${branch}, applies the PR, and confirms the fix in the isolated Docker sandbox.`}>
      {busy === "queue" ? "Queuing…" : v || status ? "↻ Re-verify" : "🧪 Queue for sandbox verification"}
    </button>
  );
}

/** The expanded detail for the "Test run" check row: the queue/
 *  run request's live state plus the stored VERIFY record. Renders the stored
 *  outcome verbatim — gates.py is the one policy; this never recomputes or
 *  softens it — and draws the trust boundary visually: host-observed exit
 *  codes are facts, the blind verdict and reason-match ratings are agent
 *  judgments, and the output tails are untrusted text from the PR itself. */
export function VerifyBody({ v, req, runner, repo }: {
  v: VerifyDetail | null;
  req: VerifyRequest | null;
  runner: VerifyRunner | null;
  repo: string | null;
}) {
  const branch = useRepoMeta().meta?.default_branch ?? "the default branch";
  return (
    <>
      {req && <RequestStrip req={req} runner={runner} />}
      {!v && !req && (
        <div className="muted small">
          Not run. Queue this PR to reproduce its claimed bug on pinned {branch} and
          confirm the fix red→green in the isolated sandbox.
        </div>
      )}
      {v && <VerifyRecord v={v} repo={repo} />}
    </>
  );
}

/** The stored VERIFY record (dynamic verification in the secretless sandbox). */
function VerifyRecord({ v, repo }: { v: VerifyDetail; repo: string | null }) {
  const baseSha = v.against_base_sha;
  const baseUrl = repo && baseSha ? `https://github.com/${repo}/commit/${baseSha}` : null;
  const branch = useRepoMeta().meta?.default_branch ?? "the default branch";

  return (
    <>
      <div className={`verdict-banner ${LEVEL_TONE[v.level]}`}>
        <span className="vb-icon">{LEVEL_ICON[v.level]}</span>
        <div>
          <div className="vb-headline">
            {v.headline}
            <FaultBadge fault={v.fault} />
            <span className="chip chip-muted sm" style={{ marginLeft: 8 }}
              title={v.outcome ? `state: ${v.state} · raw outcome: ${v.outcome}` : v.state}>{v.state}</span>
            {v.stale_reason && <span className="chip chip-yellow sm" style={{ marginLeft: 6 }} title={`This record is ${v.stale_reason} — the merge gate treats it as not run.`}>⟳ stale</span>}
            <LaneChips lanes={v.signals?.lanes} />
          </div>
          {v.cause && <div className="vb-cause">{v.cause}</div>}
          <div className="vb-detail">{v.detail}</div>
          <div className="verify-meta muted small">
            {v.tier != null && <>tier {v.tier} · </>}
            against {branch} @{" "}
            {baseUrl
              ? <a href={baseUrl} target="_blank" rel="noreferrer" title={`The pinned upstream ${branch} SHA this batch was verified against`}><code>{baseSha?.slice(0, 12)}</code></a>
              : <code>{baseSha?.slice(0, 12) ?? "?"}</code>}
            {v.checked_at && <span title={localDateTime(v.checked_at)}> · {timeAgo(v.checked_at)}</span>}
          </div>
        </div>
      </div>

      <Story v={v} />

      {/* The judge's findings — observations worth a human's read, not verdicts. */}
      {v.findings.length > 0 && (
        <div className="verify-findings">
          <div className="verify-section-head">Findings:</div>
          {v.findings.map((f, i) => (
            <Collapsible key={i} tone="muted"
              summary={<>📎 {f.title ?? "finding"}{f.confidence && <span className="muted small"> · {f.confidence} confidence</span>}</>}>
              <div className="finding-detail">{f.detail}</div>
            </Collapsible>
          ))}
        </div>
      )}
    </>
  );
}
