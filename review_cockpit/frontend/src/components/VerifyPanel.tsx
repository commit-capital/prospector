import type { VerifyDetail, VerifyFault, VerifyLevel, VerifyReasonMatch, VerifyRequest, VerifyRunner, VerifySignals, VerifyStep } from "../api";
import { Collapsible } from "./Collapsible";
import { localDateTime, timeAgo } from "../timeAgo";

/** The operator's #1 question when verification isn't green: whose fault is it?
 *  A leading badge answers it before any detail — PR-fault and harness-fault
 *  must never look alike. */
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

function ExitFact({ label, code, expected, decode }: {
  label: string; code: number | null | undefined; expected?: number; decode: Record<number, string>;
}) {
  if (code == null) return null;
  const decoded = decode[code] ?? "infrastructure error — never a signal";
  const tone = expected === undefined ? "" : code === expected ? " ok" : " bad";
  return (
    <span className={`verify-exit${tone}`} title={`Exit code observed by the trusted host from docker run — untrusted code cannot forge it. ${code} = ${decoded}.`}>
      {label} <b>{code}</b> <span className="muted">{decoded}</span>
    </span>
  );
}

const STEP_ICON: Record<VerifyStep["result"], string> = {
  pass: "✓", fail: "✗", warn: "⚠", skip: "–", info: "ℹ",
};

/** The run's checks in order, in plain English — the first thing to read. The
 *  step that broke is highlighted and carries the judge's explanation inline.
 *  `steps` is nullable: a backend from before the field existed omits it. */
function Story({ steps }: { steps: VerifyStep[] | null | undefined }) {
  if (!steps || steps.length === 0) return null;
  return (
    <div className="verify-story">
      {steps.map((st) => (
        <div key={st.key} className={`vstep vstep-${st.result}`}>
          <span className="vstep-icon">{STEP_ICON[st.result] ?? "·"}</span>
          <div className="vstep-body">
            <div className="vstep-label">{st.label}</div>
            <div className="vstep-note">{st.note}</div>
            {st.reasoning && (
              <Collapsible defaultOpen={st.result === "fail"} summary={<>judge's explanation</>}>
                <div className="verify-reasoning vstep-reasoning">{st.reasoning}</div>
              </Collapsible>
            )}
          </div>
        </div>
      ))}
    </div>
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
  if (req.status === "queued") {
    return (
      <div className="verdict-banner v-unknown">
        <span className="vb-icon">⏳</span>
        <div>
          <div className="vb-headline">
            Queued for sandbox verification
            {offline && <span className="chip chip-yellow sm" style={{ marginLeft: 8 }} title="No verification worker has beat recently, so the queue is not draining. Start the cockpit backend with TRIAGE_VERIFY_WORKER=1 on the sandbox machine.">⚠ runner offline</span>}
          </div>
          <div className="vb-detail">
            Waiting for the runner{req.source === "auto" && " · auto-picked"}
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
            {offline && <span className="chip chip-yellow sm" style={{ marginLeft: 8 }} title="No verification worker has beat recently, so the queue is not draining. Start the cockpit backend with TRIAGE_VERIFY_WORKER=1 on the sandbox machine.">⚠ runner offline</span>}
          </div>
          <div className="vb-detail">
            {req.error ?? "The runner has no usable pinned base yet"}. The worker
            retries automatically once a prepare-base run lands (the wait is
            bounded, then this errors)
            {req.queued_at && <span title={localDateTime(req.queued_at)}> · queued {timeAgo(req.queued_at)}</span>}
            {req.source === "auto" && " · auto-picked"}
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
            {req.source === "auto" && " · auto-picked"}
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
            {req.error_kind && <span className="chip chip-muted sm" style={{ marginLeft: 8 }}>{req.error_kind}</span>}
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

/** The queue/re-verify/cancel control for the "Dynamic verification" check row
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
      title="Queue this PR for sandbox verification: the runner reproduces the claimed bug on pinned main, applies the PR, and confirms the fix in the isolated Docker sandbox.">
      {busy === "queue" ? "Queuing…" : v || status ? "↻ Re-verify" : "🧪 Queue for sandbox verification"}
    </button>
  );
}

/** The expanded detail for the "Dynamic verification" check row: the queue/
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
  return (
    <>
      {req && <RequestStrip req={req} runner={runner} />}
      {!v && !req && (
        <div className="muted small">
          Not run. Queue this PR to reproduce its claimed bug on pinned main and
          confirm the fix red→green in the isolated sandbox.
        </div>
      )}
      {v && <VerifyRecord v={v} repo={repo} />}
    </>
  );
}

/** The stored VERIFY record (dynamic verification in the secretless sandbox). */
function VerifyRecord({ v, repo }: { v: VerifyDetail; repo: string | null }) {
  const s = v.signals ?? {};
  const blind = s.blind_adequacy;
  const rg = s.red_green;
  const repro = s.independent_repro;
  const authored = s.authored_test;
  const redMatch = matchSummary(s.red_reason_match);
  const reproMatch = matchSummary(s.repro_reason_match);
  // an escalation exists to be read — open the judgment content by default
  const open = v.level === "attention";
  const baseSha = v.against_base_sha;
  const baseUrl = repo && baseSha ? `https://github.com/${repo}/commit/${baseSha}` : null;

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
            against main @{" "}
            {baseUrl
              ? <a href={baseUrl} target="_blank" rel="noreferrer" title="The pinned upstream main SHA this batch was verified against"><code>{baseSha?.slice(0, 12)}</code></a>
              : <code>{baseSha?.slice(0, 12) ?? "?"}</code>}
            {v.checked_at && <span title={localDateTime(v.checked_at)}> · {timeAgo(v.checked_at)}</span>}
          </div>
        </div>
      </div>

      <Story steps={v.story} />

      {/* Signal 2 — host-observed exit codes: the trusted facts. Everything
          below them is judgment or untrusted evidence. */}
      {rg && (
        <div className="verify-exits">
          <span className="verify-exits-label" title="Exit codes the trusted host observed from each phase container. Untrusted code cannot forge its own PID-1 exit, so these are the phase's facts; a red is accepted only on exactly 20.">
            🔒 host-observed exit codes
          </span>
          <ExitFact label="apply patch" code={rg.apply_exit} expected={0} decode={APPLY_DECODE} />
          <ExitFact label="test without fix" code={rg.red_exit} expected={20} decode={RED_DECODE} />
          <ExitFact label="test with fix" code={rg.green_exit} expected={0} decode={GREEN_DECODE} />
        </div>
      )}

      {/* Signal 1 — the blind adequacy verdict (agent judgment). */}
      {blind && (
        <Collapsible defaultOpen={open}
          tone={blind.faithful === false ? "red" : blind.faithful ? "green" : "muted"}
          summary={<>
            🕶️ Pre-run review — {blind.faithful === false ? "the PR's test was judged NOT a faithful repro of the bug" : blind.faithful ? "the PR's test was judged a faithful repro of the bug" : "no verdict"}
            {blind.confidence && <span className="muted"> · {blind.confidence} confidence</span>}
            <span className="chip chip-blue sm" style={{ marginLeft: 8 }} title="This judgment was committed to the store before any container ran — the phase's integrity property. It cannot have been rationalized backward from a green result.">committed before any run</span>
          </>}>
          {blind.claimed_symptom && (
            <div className="verify-field"><span className="verify-field-label">Claimed symptom</span>{blind.claimed_symptom}</div>
          )}
          {blind.reasoning && (
            <div className="verify-field"><span className="verify-field-label">Reasoning</span><span className="verify-reasoning">{blind.reasoning}</span></div>
          )}
          {blind.test_cmd && (
            <div className="verify-field"><span className="verify-field-label">Test command</span><code className="verify-cmd">{blind.test_cmd}</code></div>
          )}
          {(blind.requires_live_agent || blind.from_linked_issue || blind.has_test === false) && (
            <div className="verify-field muted small">
              {blind.has_test === false && <>ships no test · </>}
              {blind.requires_live_agent && <>reproducing needs a live agent (Tier 2) · </>}
              {blind.from_linked_issue && <>repro taken from the linked issue</>}
            </div>
          )}
        </Collapsible>
      )}

      {/* Signal 3 — the comparison the phase turns on: the pre-committed
          prediction vs. the red output that actually happened. */}
      {(blind?.expected_red_signature || rg?.red_output_tail) && (
        <Collapsible defaultOpen={open}
          tone={redMatch.tone === "green" ? "green" : redMatch.tone === "red" ? "red" : "muted"}
          summary={<>🔬 Failure-reason check — did the test fail the way the bug predicts? {redMatch.icon} {redMatch.text}</>}>
          {s.red_reason_match?.reasoning && (
            <div className="verify-field"><span className="verify-field-label">Judge's reasoning</span><span className="verify-reasoning">{s.red_reason_match.reasoning}</span></div>
          )}
          <div className="verify-compare">
            <div>
              <div className="verify-block-label">📌 predicted red signature <span className="chip chip-blue sm" title="The blind pass's prediction of what the red output should look like, recorded before any result existed.">pre-committed</span></div>
              <Tail text={blind?.expected_red_signature} />
            </div>
            <div>
              <div className="verify-block-label">captured red output <UntrustedTag /></div>
              <Tail text={rg?.red_output_tail} />
            </div>
          </div>
          {rg?.green_output_tail && (
            <Collapsible summary={<>green run output <UntrustedTag /></>}>
              <Tail text={rg.green_output_tail} />
            </Collapsible>
          )}
        </Collapsible>
      )}

      {/* Signal 4 (+ its reason check) — corroborating evidence, never a gate. */}
      {repro && (
        <Collapsible
          tone={reproMatch.tone === "green" ? "green" : reproMatch.tone === "red" ? "yellow" : "muted"}
          summary={<>
            🔁 Independent repro — {repro.ran ? <>ran, exit <b>{repro.exit_code}</b> · {reproMatch.icon} {reproMatch.text}</> : "did not run"}
            <span className="chip chip-muted sm" style={{ marginLeft: 8 }} title="The agent's own reproduction, run against pinned main. Corroborating evidence only — it never changes the outcome.">corroborating, not a gate</span>
          </>}>
          {s.repro_reason_match?.reasoning && (
            <div className="verify-field"><span className="verify-field-label">Judge's reasoning</span><span className="verify-reasoning">{s.repro_reason_match.reasoning}</span></div>
          )}
          {blind?.expected_repro_signature && (
            <div className="verify-field"><span className="verify-field-label">Predicted repro signature</span><span className="verify-reasoning">{blind.expected_repro_signature}</span></div>
          )}
          {blind?.repro_command && (
            <div className="verify-field"><span className="verify-field-label">Repro command <span className="muted small">(agent-authored)</span></span><code className="verify-cmd">{blind.repro_command}</code></div>
          )}
          {repro.output_tail && (
            <div className="verify-field">
              <div className="verify-block-label">repro output <UntrustedTag /></div>
              <Tail text={repro.output_tail} />
            </div>
          )}
          {repro.from_linked_issue && <div className="muted small">Repro steps taken from the linked issue.</div>}
        </Collapsible>
      )}

      {/* The agent-authored test lane (no-test PRs) — corroborating evidence
          only; the outcome copy and story carry the policy context. */}
      {authored?.attempted && (
        <Collapsible defaultOpen={v.outcome === "agent-verified"}
          tone={v.outcome === "agent-verified" ? "green" : "yellow"}
          summary={<>
            🧪 Agent-authored test — {authored.skipped_reason
              ? <>not runnable ({authored.skipped_reason})</>
              : authored.red_exit == null
                ? "authored, never ran"
                : <>ran red→green: red <b>{authored.red_exit}</b> · green <b>{authored.green_exit}</b></>}
            <span className="chip chip-muted sm" style={{ marginLeft: 8 }} title="The PR ships no test, so the harness authored one. The agent wrote file contents only; the driver validated the paths (new test files only), built the patch, and derived the command itself. Corroborating evidence — it never counts toward the auto-merge bar.">corroborating, not a gate</span>
            <span className="chip chip-blue sm" style={{ marginLeft: 6 }} title="The authored files and the predicted failure were committed to the store before any container ran — they cannot have been rationalized backward from a result.">committed before any run</span>
          </>}>
          {authored.reasoning && (
            <div className="verify-field"><span className="verify-field-label">Authoring reasoning</span><span className="verify-reasoning">{authored.reasoning}</span></div>
          )}
          {authored.test_cmd && (
            <div className="verify-field"><span className="verify-field-label">Derived command <span className="muted small">(driver-derived from the authored paths)</span></span><code className="verify-cmd">{authored.test_cmd}</code></div>
          )}
          {authored.expected_red_signature && (
            <div className="verify-field"><span className="verify-field-label">Predicted red signature</span><span className="verify-reasoning">{authored.expected_red_signature}</span></div>
          )}
          {(authored.red_exit != null || authored.green_exit != null) && (
            <div className="verify-exits">
              <span className="verify-exits-label">🔒 host-observed exit codes</span>
              <ExitFact label="authored test without fix" code={authored.red_exit} expected={20} decode={RED_DECODE} />
              <ExitFact label="authored test with fix" code={authored.green_exit} expected={0} decode={GREEN_DECODE} />
              <ExitFact label="confirm red" code={authored.red_exit_confirm} expected={20} decode={RED_DECODE} />
              <ExitFact label="confirm green" code={authored.green_exit_confirm} expected={0} decode={GREEN_DECODE} />
            </div>
          )}
          {(authored.files ?? []).map((f) => (
            <Collapsible key={f.path} summary={<>authored file <code>{f.path}</code></>}>
              <pre className="verify-tail">{f.contents}</pre>
            </Collapsible>
          ))}
          {authored.red_output_tail && (
            <Collapsible summary={<>authored red output <UntrustedTag /></>}>
              <Tail text={authored.red_output_tail} />
            </Collapsible>
          )}
          {authored.green_output_tail && (
            <Collapsible summary={<>authored green output <UntrustedTag /></>}>
              <Tail text={authored.green_output_tail} />
            </Collapsible>
          )}
        </Collapsible>
      )}

      {/* The judge's findings — observations worth a human's read, not verdicts. */}
      {v.findings.length > 0 && (
        <div className="verify-findings">
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
