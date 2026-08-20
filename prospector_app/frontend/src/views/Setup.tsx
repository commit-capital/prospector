import { useCallback, useEffect, useState } from "react";
import { api, type SetupCheck, type SetupReadiness, type WorkerFlags } from "../api";

/** How often the readiness rows re-check while the page is open. Fast enough
 *  that rows turn green as setup-worker-machine.sh works through its steps. */
const POLL_MS = 5000;

const COMMAND = "./setup-worker-machine.sh";

/** The lane switches, in the order a machine is provisioned. `needs` names a
 *  readiness check that must pass first — the autofix lane is meaningless
 *  without a push identity, so its control says so rather than failing later. */
const SWITCHES: { key: string; label: string; hint: string; needs?: string }[] = [
  { key: "TRIAGE_VERIFY_WORKER", label: "Run verification", hint: "drain the sandbox verify queue" },
  { key: "TRIAGE_VERIFY_AUTOHUNT", label: "Hunt while idle", hint: "security-review and verify clean candidates unprompted" },
  { key: "TRIAGE_FIX_WORKER", label: "Run autofix", hint: "drain the fix queue", needs: "push_identity" },
  { key: "TRIAGE_FIX_AUTOHUNT", label: "Queue fixes while idle", hint: "find PRs needing a rebase or base merge", needs: "push_identity" },
];

function CheckRow({ check }: { check: SetupCheck }) {
  const tone = check.ok ? "chip chip-green sm" : check.blocking ? "chip chip-red sm" : "chip chip-amber sm";
  return (
    <tr>
      <td><span className={tone}>{check.ok ? "ready" : check.blocking ? "missing" : "optional"}</span></td>
      <td>{check.label}</td>
      <td className="muted small">
        {check.detail}
        {check.remedy && <> — <strong>{check.remedy}</strong></>}
      </td>
    </tr>
  );
}

export default function Setup() {
  const [readiness, setReadiness] = useState<SetupReadiness | null>(null);
  const [flags, setFlags] = useState<WorkerFlags>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api.setupReadiness();
      setReadiness(r.readiness);
      setFlags(r.flags);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  // A self-scheduling poll rather than an interval: the next read is queued only
  // once the last one lands, so a slow check never stacks requests behind it.
  useEffect(() => {
    let live = true;
    let timer: number | undefined;
    const poll = async () => {
      if (!live) return;
      await load();
      if (live) timer = window.setTimeout(() => void poll(), POLL_MS);
    };
    timer = window.setTimeout(() => void poll(), 0);
    return () => { live = false; if (timer != null) clearTimeout(timer); };
  }, [load]);

  async function toggle(key: string, on: boolean) {
    setBusy(key);
    setError(null);
    try {
      const r = await api.setSetupFlags({ [key]: on ? "1" : "" });
      setFlags(r.applied.flags);
      setReadiness(r.readiness);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  if (error && !readiness) return <div className="pad"><p className="chip chip-red">{error}</p></div>;
  if (!readiness) return <div className="pad muted">reading this machine…</div>;

  const byKey = Object.fromEntries(readiness.checks.map((c) => [c.key, c]));
  const blockers = readiness.checks.filter((c) => !c.ok && c.blocking);

  return (
    <div className="pad">
      <h2>🛠️ Setup — {readiness.host}</h2>
      <p className="muted small">
        This page configures the machine serving it. Every machine that processes
        work is provisioned on its own; the Control tab reports the whole fleet.
      </p>

      <h3>
        {readiness.ready
          ? <span className="chip chip-green">processing work</span>
          : <span className="chip chip-red">not processing work</span>}
        {" "}
        {readiness.autofix_ready && <span className="chip chip-green">autofix on</span>}
      </h3>

      {blockers.length > 0 && (
        <section>
          <p>
            {blockers.length === 1 ? "One thing is" : `${blockers.length} things are`} missing.
            One command fixes all of them — run it in your own terminal, from the repo root:
          </p>
          <pre className="log-tail">{COMMAND}</pre>
          <button
            onClick={() => { void navigator.clipboard.writeText(COMMAND); setCopied(true); }}
          >
            {copied ? "copied" : "copy command"}
          </button>
          <p className="muted small">
            It installs what is missing, builds this machine's own sandbox base,
            and turns the lanes on. Re-running it on a ready machine changes nothing.
          </p>
        </section>
      )}

      <h3>This machine</h3>
      <table className="rows">
        <tbody>
          {readiness.checks.map((c) => <CheckRow key={c.key} check={c} />)}
        </tbody>
      </table>

      <h3>Lanes</h3>
      <p className="muted small">
        Written to this machine's <code>.env</code> and applied to the running
        worker immediately. Only these switches are writable from here.
      </p>
      {SWITCHES.map((s) => {
        const blocked = s.needs != null && byKey[s.needs] != null && !byKey[s.needs].ok;
        return (
          <div key={s.key}>
            <label>
              <input
                type="checkbox"
                checked={flags[s.key] === "1"}
                disabled={busy != null || blocked}
                onChange={(e) => void toggle(s.key, e.target.checked)}
              />
              {" "}{s.label}
            </label>
            {" "}<span className="muted small">
              {blocked ? `needs ${byKey[s.needs!].label.toLowerCase()}` : s.hint}
            </span>
          </div>
        );
      })}

      {error && <p className="chip chip-red sm">{error}</p>}
    </div>
  );
}
