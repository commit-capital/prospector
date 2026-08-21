import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router";
import { api, type SetupCheck, type SetupReadiness, type WorkerFlags } from "../api";
import { useRepoMeta } from "../RepoMetaContext";

/** How often the readiness rows re-check while the page is open. Fast enough
 *  that rows turn green as setup-worker-machine.sh works through its steps. */
const POLL_MS = 5000;

const COMMAND = "./setup-worker-machine.sh";

/** The lane switches, in the order a machine is provisioned. `needs` names a
 *  readiness check that must pass first — the autofix lane is meaningless
 *  without a push identity, so its control says so rather than failing later. */
const SWITCHES: { key: string; label: string; hint: string; needs?: string }[] = [
  { key: "TRIAGE_VERIFY_WORKER", label: "Test pull requests",
    hint: "run each queued pull request's tests in a sandbox to prove the fix works — failing before the change, passing after" },
  { key: "TRIAGE_VERIFY_AUTOHUNT", label: "Look for work on its own",
    hint: "when the queue is empty, pick clean pull requests and run security reviews and verification on them unprompted" },
  { key: "TRIAGE_FIX_WORKER", label: "Prepare fixes",
    hint: "update, rebase, and draft fixes for contributors' branches — each result is parked here for approval before anything is pushed",
    needs: "push_identity" },
  { key: "TRIAGE_FIX_AUTOHUNT", label: "Queue fixes on its own",
    hint: "notice pull requests that have fallen behind their base branch and queue the update or rebase itself",
    needs: "push_identity" },
];

/** What each readiness check's subject is for, in words for someone meeting it
 *  for the first time. Keyed by the check keys `worker_readiness.checks` emits. */
const EXPLAIN: Record<string, string> = {
  docker: "A container runtime. Every test and build runs inside a disposable container, so pull-request code never runs directly on this machine.",
  sandbox_image: "The locked-down container image those runs use — it carries no credentials and blocks the network.",
  base_pin: "This machine's own copy of the repository's default branch, the before/after baseline every verification is proven against.",
  verify_flag: "The background process that picks up queued verification work.",
  push_identity: "A dedicated GitHub user whose SSH key pushes fixes to contributors' branches. Only autofix needs it.",
  fix_flag: "The background process that prepares branch updates, rebases, and fixes.",
};

/** The lane flags whose presence means this machine has been signed up to
 *  process work. Readiness answers "can it run right now", which a provisioned
 *  machine fails whenever Docker is down; opting in is what these record. */
const OPT_IN_FLAGS = ["TRIAGE_VERIFY_WORKER", "TRIAGE_FIX_WORKER"];

function CheckRow({ check }: { check: SetupCheck }) {
  const tone = check.ok ? "chip chip-green sm" : check.blocking ? "chip chip-red sm" : "chip chip-amber sm";
  return (
    <tr>
      <td><span className={tone}>{check.ok ? "ready" : check.blocking ? "needed" : "optional"}</span></td>
      <td>
        {check.label}
        {EXPLAIN[check.key] && <div className="muted small">{EXPLAIN[check.key]}</div>}
      </td>
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
  // `?provision=1` opens the provisioning steps directly — the wizard's
  // "Set this computer up" link lands here already expanded.
  const [params] = useSearchParams();
  const [expanded, setExpanded] = useState(params.get("provision") === "1");

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

  async function toggle(updates: Record<string, boolean>) {
    setBusy(Object.keys(updates).join(","));
    setError(null);
    try {
      const r = await api.setSetupFlags(
        Object.fromEntries(Object.entries(updates).map(([k, on]) => [k, on ? "1" : ""])));
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

  const optedIn = OPT_IN_FLAGS.some((k) => flags[k] === "1");

  return (
    <div className="pad">
      <h2>🛠️ Setup — {readiness.host}</h2>
      <p className="muted small">
        This page configures the machine serving it. Every machine that processes
        work is provisioned on its own; the Control tab reports the whole fleet.
      </p>

      {optedIn || expanded
        ? <WorkerSection readiness={readiness} flags={flags} busy={busy} onToggle={toggle} />
        : <ProvisionBanner onStart={() => setExpanded(true)} />}

      <ShareSection />

      {error && <p className="chip chip-red sm">{error}</p>}
    </div>
  );
}

/** The offer a machine that has never opted in sees in place of the worker
 *  controls: what accepting costs, and one button that reveals the rest. */
function ProvisionBanner({ onStart }: { onStart: () => void }) {
  const { meta } = useRepoMeta();
  return (
    <section className="setup-card setup-offer">
      <h3>⚙️ Provision this computer to run automated tasks{meta && <> on {meta.display_name}</>}</h3>
      <p className="muted small">
        Runs a heavy background process that analyzes, tests, fixes, and iterates
        on pull requests, issues, and advisories in a sandboxed environment on
        this machine.
      </p>
      <button className="btn-primary" onClick={onStart}>set this computer up</button>
    </section>
  );
}

/** Everything about this machine as a work processor: whether it is running,
 *  what it still needs, and which work it does. A machine that has never opted
 *  in gets a first-time framing; a signed-up machine gets its status plainly. */
function WorkerSection(
  { readiness, flags, busy, onToggle }: {
    readiness: SetupReadiness;
    flags: WorkerFlags;
    busy: string | null;
    onToggle: (updates: Record<string, boolean>) => void;
  },
) {
  const [copied, setCopied] = useState(false);
  const byKey = Object.fromEntries(readiness.checks.map((c) => [c.key, c]));
  const blockers = readiness.checks.filter((c) => !c.ok && c.blocking);
  const optedIn = OPT_IN_FLAGS.some((k) => flags[k] === "1");
  const available = SWITCHES.filter(
    (s) => s.needs == null || byKey[s.needs] == null || byKey[s.needs].ok);
  const allOn = available.length > 0 && available.every((s) => flags[s.key] === "1");

  return (
    <>
      {optedIn && (
        <h3>
          {readiness.ready
            ? <span className="chip chip-green">processing work</span>
            : <span className="chip chip-red">not processing work</span>}
          {" "}
          {readiness.autofix_ready && <span className="chip chip-green">autofix on</span>}
        </h3>
      )}

      {!optedIn && (
        <p className="muted small">
          To process pull requests and issues, this machine runs their tests,
          security reviews, and fixes inside disposable containers. Here is
          everything that takes, and where this machine stands on each piece:
        </p>
      )}
      {optedIn && blockers.length > 0 && (
        <p className="muted small">
          This machine has signed up for work but cannot process it right now —
          the {blockers.length === 1 ? "row marked" : "rows marked"} “needed”
          below {blockers.length === 1 ? "says" : "say"} why.
        </p>
      )}

      {blockers.length > 0 && (
        <section>
          <p>
            One command installs everything missing and turns the work on — run
            it in your own terminal, from the repo root:
          </p>
          <pre className="log-tail">{COMMAND}</pre>
          <button className="btn-secondary"
            onClick={() => { void navigator.clipboard.writeText(COMMAND); setCopied(true); }}
          >
            {copied ? "copied" : "copy command"}
          </button>
          <p className="muted small">
            First-time setup takes roughly 10&nbsp;GB of disk and 15–30 minutes,
            most of it building this machine's sandbox base — the rows below turn
            green as it works through them. While the worker is on, its container
            runtime keeps a virtual machine running that reserves 12&nbsp;GB of
            memory. Re-running the command on a ready machine changes nothing.
          </p>
        </section>
      )}

      <table className="rows">
        <tbody>
          {readiness.checks.map((c) => <CheckRow key={c.key} check={c} />)}
        </tbody>
      </table>

      <h3>What work should this machine do?</h3>
      <p className="muted small">
        Saved on this machine and applied immediately. Choices that need
        something not yet set up stay off until it is.
      </p>
      <div>
        <label>
          <input
            type="checkbox"
            checked={allOn}
            disabled={busy != null || available.length === 0}
            onChange={(e) =>
              onToggle(Object.fromEntries(available.map((s) => [s.key, e.target.checked])))}
          />
          {" "}<strong>Everything it can</strong>
        </label>
        {" "}<span className="muted small">all of the below</span>
      </div>
      {SWITCHES.map((s) => {
        const blocked = s.needs != null && byKey[s.needs] != null && !byKey[s.needs].ok;
        return (
          <div key={s.key}>
            <label>
              <input
                type="checkbox"
                checked={flags[s.key] === "1"}
                disabled={busy != null || blocked}
                onChange={(e) => onToggle({ [s.key]: e.target.checked })}
              />
              {" "}{s.label}
            </label>
            {" "}<span className="muted small">
              {blocked ? `needs the ${byKey[s.needs!].label.toLowerCase()} above` : s.hint}
            </span>
          </div>
        );
      })}
    </>
  );
}

/** Copies the deployment bundle a teammate pastes into their setup wizard. It
 *  carries the store URL, so it is a credential and the card says so. The
 *  bot's private key rides along only when the checkbox opts in. */
function ShareSection() {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const [includeKey, setIncludeKey] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const copy = async () => {
    setProblem(null);
    try {
      const { bundle } = await api.setupShare(includeKey);
      await navigator.clipboard.writeText(bundle);
      setState("copied");
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
      setState("failed");
    }
    setTimeout(() => setState("idle"), 3000);
  };

  return (
    <section className="setup-card setup-share">
      <h3>🤝 Invite a member to this project</h3>
      <p className="muted small">
        Copies everything a fresh
        checkout needs to join this deployment: the repo, bot identity, review
        config, the store URL, and this deployment's <code>profile.json</code>.
        Your teammate pastes it into the setup wizard their app opens on first
        run.
      </p>
      <label className="small">
        <input type="checkbox" checked={includeKey}
          onChange={(e) => setIncludeKey(e.target.checked)} />
        {" "}Also let the teammate act as the bot — includes the App's private
        key, so approved actions execute for real from their machine too.
      </label>
      <p className="setup-warn small">
        ⚠ This carries the database password{includeKey ? " and the bot's private key" : ""}.
        Send it through a password manager or a direct message — never a
        channel with history.
      </p>
      <div>
        <button className="btn-secondary" onClick={() => void copy()}>
          {state === "copied" ? "copied ✓" : state === "failed" ? "copy failed" : "copy setup for a teammate"}
        </button>
        {problem && <span className="chip chip-red sm">{problem}</span>}
      </div>
    </section>
  );
}
