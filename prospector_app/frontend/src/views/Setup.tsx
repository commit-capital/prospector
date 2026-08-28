import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router";
import {
  api,
  type PushAccount,
  type PushKeyInfo,
  type SetupCheck,
  type SetupReadiness,
  type WorkerFlags,
} from "../api";
import type { AgentPick } from "../agentProvider";
import { AgentProviderChooser } from "../components/AgentProviderChooser";
import { useRepoMeta } from "../RepoMetaContext";

/** How often the readiness rows re-check while the page is open. Fast enough
 *  that rows turn green as setup-worker-machine.sh works through its steps. */
const POLL_MS = 5000;

const COMMAND = "./setup-worker-machine.sh";
const TEARDOWN = "./teardown-worker-machine.sh";

/** What `teardown-worker-machine.sh` removes beyond the lane switches, one
 *  option per checkbox; the page composes the command from them. */
const TEARDOWN_PARTS: { flag: "artifacts" | "vm" | "packages"; label: string; hint: string }[] = [
  { flag: "artifacts", label: "Remove this machine's sandbox artifacts",
    hint: "the base images, the hardened sandbox image, the scratch clone under ~/.pr-triage-verify, and this machine's base pin in the store — several gigabytes" },
  { flag: "vm", label: "Delete the Docker VM",
    hint: "stops and deletes the Colima VM, which reserves 12 GB of memory while it runs" },
  { flag: "packages", label: "Uninstall the container runtime",
    hint: "brew uninstall colima and docker; gh, jq, and node stay, other things use them" },
];

/** The lane switches, in the order a machine is provisioned. `needs` names the
 *  readiness checks that must pass first — the autofix lane is meaningless
 *  without a push identity, and an unattended agent fix without the profile's
 *  opt-in, so each control says so rather than failing later. */
/** A plain switch writes "1" when ticked. A switch with `parts` owns those
 *  names inside its key's comma-separated value, so two switches can share
 *  TRIAGE_FIX_AUTOPUSH; `id` tells them apart in the UI. */
const SWITCHES: { key: string; id?: string; label: string; hint: string;
                  needs?: string[]; parts?: string[] }[] = [
  { key: "TRIAGE_VERIFY_WORKER", label: "Test pull requests",
    hint: "when someone queues a pull request for testing, run its tests here in a sealed-off container to prove the fix works: they fail before the change and pass after" },
  { key: "TRIAGE_VERIFY_AUTOHUNT", label: "Look for work on its own",
    hint: "when nothing is queued, pick healthy pull requests and run security reviews and tests on them without being asked" },
  { key: "TRIAGE_FIX_WORKER", label: "Prepare fixes",
    hint: "when someone clicks a fix in the app, do the work here — bring the contributor's branch up to date, or have the AI draft a fix. Every result waits here for a person's approval before anything is pushed",
    needs: ["push_identity"] },
  { key: "TRIAGE_FIX_AUTOHUNT", label: "Queue branch updates on its own",
    hint: "spot pull requests that have fallen behind the main branch and queue the branch update without being asked",
    needs: ["push_identity"] },
  { key: "TRIAGE_FIX_HUNT_FIX", label: "Draft fixes on its own",
    hint: "spot pull requests that pass their tests but scored below the review bar, and have the AI draft a fix without being asked — or, when the reviewer's only complaint is the description, a new description that follows the template. One try per version of the pull request, and each draft waits for approval",
    needs: ["push_identity", "fix_policy"] },
  { key: "TRIAGE_FIX_HUNT_RESOLVE", label: "Resolve merge conflicts on its own",
    hint: "when a branch update it queued runs into a real conflict, have the AI resolve it instead of giving up — the result waits for approval, with its reasoning per file",
    needs: ["push_identity"] },
  { key: "TRIAGE_FIX_AUTOPUSH", parts: ["update", "rebase"], label: "Push branch updates without asking",
    hint: "a branch update or rebase that passes the build check is pushed straight to the contributor's branch instead of waiting here for approval. AI-drafted fixes always wait",
    needs: ["push_identity"] },
  { key: "TRIAGE_FIX_AUTOPUSH", id: "TRIAGE_FIX_AUTOPUSH:resolve", parts: ["resolve"],
    label: "Push agent-resolved conflicts without asking",
    hint: "an AI conflict resolution is pushed only after two independent AI reviewers both fail to find anything wrong with it, the tests related to the conflicted files pass in the sandbox, and the files are not high-risk — anything less waits here for you, with the reviewers' reasons",
    needs: ["push_identity"] },
];

const switchId = (s: { key: string; id?: string }): string => s.id ?? s.key;
/** Stable ordering for the composed TRIAGE_FIX_AUTOPUSH value. */
const AUTOPUSH_ORDER = ["update", "rebase", "fix", "describe", "resolve"];
const valueParts = (value: string): Set<string> =>
  new Set(value.split(",").map((p) => p.trim()).filter(Boolean));
const isOn = (flags: WorkerFlags, s: { key: string; parts?: string[] }): boolean =>
  s.parts
    ? s.parts.every((p) => valueParts(flags[s.key] ?? "").has(p))
    : (flags[s.key] ?? "") !== "";

/** What each readiness check's subject is for, in words for someone meeting it
 *  for the first time. Keyed by the check keys `worker_readiness.checks` emits. */
const EXPLAIN: Record<string, string> = {
  docker: "The program that runs sealed-off, throwaway containers. Every pull request is tested inside one, so its code never runs directly on this computer.",
  sandbox_image: "The sealed-off container those tests run in. It holds no passwords or keys and cannot reach the internet.",
  base_pin: "This computer's own copy of the project's main branch — the “before” that each pull request is compared against to prove its fix works.",
  verify_flag: "The background job on this computer that tests queued pull requests.",
  push_identity: "The GitHub account that fixes are pushed to contributors' branches under — yours, or a separate one. Only needed for fixing pull requests, not for testing them.",
  fix_flag: "The background job on this computer that updates branches and drafts fixes.",
  fix_policy: "The project's permission for the AI to draft fixes without being asked: a short list in profile.json of what it may try to fix. Only “Draft fixes on its own” needs it — testing, branch updates, and fixes a person asks for all work without it.",
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
    // Updates are keyed by switch id. A key with `parts` switches (the two
    // autopush ones) is recomposed whole from its switches' final states, so
    // the page owns the entire value — a part no switch claims is cleared,
    // exactly as writing the key outright would.
    const next: Record<string, string> = {};
    for (const [id, on] of Object.entries(updates)) {
      const s = SWITCHES.find((x) => switchId(x) === id);
      if (!s) continue;
      if (s.parts) {
        const owned = SWITCHES.filter((x) => x.key === s.key && x.parts);
        const state = (x: { key: string; id?: string; parts?: string[] }): boolean =>
          switchId(x) in updates ? updates[switchId(x)] : isOn(flags, x);
        const kept = new Set(owned.filter(state).flatMap((x) => x.parts ?? []));
        next[s.key] = AUTOPUSH_ORDER.filter((p) => kept.has(p)).join(",");
      } else {
        next[s.key] = on ? "1" : "";
      }
    }
    try {
      const r = await api.setSetupFlags(next);
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
  // Artifacts on disk mean the machine was provisioned, whatever its switches
  // say now; the unprovision card lives in the worker section, so that stays.
  const provisioned = readiness.checks.some(
    (c) => (c.key === "sandbox_image" || c.key === "base_pin") && c.ok);

  return (
    <div className="pad">
      <h2>🛠️ Setup — {readiness.host}</h2>
      <p className="muted small">
        This page sets up the computer you are using right now. Each computer
        that does work is set up on its own; the Control tab shows all of them.
      </p>

      <AgentProviderSettings />

      {optedIn || expanded || provisioned
        ? <WorkerSection readiness={readiness} flags={flags} busy={busy} onToggle={toggle}
            onChanged={() => void load()} />
        : <ProvisionBanner onStart={() => setExpanded(true)} />}

      <ShareSection />

      {error && <p className="chip chip-red sm">{error}</p>}
    </div>
  );
}

type SavedAgentPick = Exclude<AgentPick, null>;

function savedAgentPick(provider: string): SavedAgentPick {
  if (provider === "claude" || provider === "codex") return provider;
  return "none";
}

function AgentProviderSettings() {
  const { meta, refresh } = useRepoMeta();
  if (!meta) return null;
  const provider = savedAgentPick(meta.agent_provider);
  return <AgentProviderCard key={provider} provider={provider} onSaved={refresh} />;
}

function AgentProviderCard({ provider, onSaved }: {
  provider: SavedAgentPick;
  onSaved: () => void;
}) {
  const [pick, setPick] = useState<AgentPick>(provider);
  const [saved, setSaved] = useState<SavedAgentPick>(provider);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const apply = async () => {
    if (pick == null || pick === saved) return;
    setBusy(true);
    setProblem(null);
    try {
      const state = await api.onboardingApply({
        step: "agent", env: { TRIAGE_AGENT_PROVIDER: pick },
      });
      const applied = state.agent_provider == null
        ? pick : savedAgentPick(state.agent_provider);
      setPick(applied);
      setSaved(applied);
      onSaved();
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="setup-card">
      <h3>🤖 In-app agent</h3>
      <p className="muted small">
        Choose the local account behind the “Ask the agent” sidebar. This
        machine keeps the choice and login to itself.
      </p>
      <AgentProviderChooser pick={pick} onPick={setPick} />
      {problem && <p className="chip chip-red sm">{problem}</p>}
      <div className="welcome-actions">
        <button className="btn-primary" disabled={busy || pick == null || pick === saved}
          onClick={() => void apply()}>
          {busy ? "saving…" : pick === saved ? "saved" : "save agent setting"}
        </button>
      </div>
    </section>
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

/** One card for everything about this machine as a work processor: whether it
 *  is running, what it still needs, and which work it does. A machine that has
 *  never opted in gets a first-time framing; a signed-up machine gets its
 *  status plainly. */
function WorkerSection(
  { readiness, flags, busy, onToggle, onChanged }: {
    readiness: SetupReadiness;
    flags: WorkerFlags;
    busy: string | null;
    onToggle: (updates: Record<string, boolean>) => void;
    onChanged: () => void;
  },
) {
  const [copied, setCopied] = useState(false);
  const byKey = Object.fromEntries(readiness.checks.map((c) => [c.key, c]));
  const blockers = readiness.checks.filter((c) => !c.ok && c.blocking);
  const optedIn = OPT_IN_FLAGS.some((k) => flags[k] === "1");
  const provisioned = !!(byKey.sandbox_image?.ok || byKey.base_pin?.ok);
  const unmet = (s: { needs?: string[] }): string[] =>
    (s.needs ?? []).filter((k) => byKey[k] != null && !byKey[k].ok);
  const available = SWITCHES.filter((s) => unmet(s).length === 0);
  const allOn = available.length > 0 && available.every((s) => isOn(flags, s));

  return (
    <section className="setup-card setup-queues">
      <h3>
        ⚙️ Automatic Work Queues
        {optedIn && (
          <>
            {" "}
            {readiness.ready
              ? <span className="chip chip-green">processing work</span>
              : <span className="chip chip-red">not processing work</span>}
            {" "}
            {readiness.autofix_ready && <span className="chip chip-green">autofix on</span>}
          </>
        )}
      </h3>

      {!optedIn && (
        <p className="muted small">
          To work on pull requests, this computer runs their tests, security
          reviews, and fixes inside sealed-off, throwaway containers. Here is
          everything that takes, and where this computer stands on each piece:
        </p>
      )}
      {optedIn && blockers.length > 0 && (
        <p className="muted small">
          This computer is signed up for work but cannot do it right now —
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

      {byKey.push_identity != null && !byKey.push_identity.ok && (
        <PushIdentitySection onDone={onChanged} />
      )}
      {byKey.fix_policy != null && !byKey.fix_policy.ok
        && (byKey.push_identity == null || byKey.push_identity.ok) && (
        <ProfileSection onDone={onChanged} />
      )}

      <h4>What work should this computer do?</h4>
      <p className="muted small">
        Saved on this computer and applied right away. A choice that needs
        something not set up yet stays off until it is.
      </p>
      <div>
        <label>
          <input
            type="checkbox"
            checked={allOn}
            disabled={busy != null || available.length === 0}
            onChange={(e) =>
              onToggle(Object.fromEntries(available.map((s) => [switchId(s), e.target.checked])))}
          />
          {" "}<strong>Everything it can</strong>
        </label>
        {" "}<span className="muted small">all of the below</span>
      </div>
      {SWITCHES.map((s) => {
        const missing = unmet(s);
        const blocked = missing.length > 0;
        return (
          <div key={switchId(s)}>
            <label>
              <input
                type="checkbox"
                checked={isOn(flags, s)}
                disabled={busy != null || blocked}
                onChange={(e) => onToggle({ [switchId(s)]: e.target.checked })}
              />
              {" "}{s.label}
            </label>
            {" "}<span className="muted small">
              {blocked
                ? `needs the ${missing.map((k) => byKey[k].label.toLowerCase()).join(" and ")} above`
                : s.hint}
            </span>
          </div>
        );
      })}

      {(optedIn || provisioned) && (
        <UnprovisionSection
          anyOn={SWITCHES.some((s) => isOn(flags, s))}
          canStart={available.length > 0}
          busy={busy}
          onStop={() => onToggle(Object.fromEntries(SWITCHES.map((s) => [switchId(s), false])))}
          onStart={() => onToggle(Object.fromEntries(available.map((s) => [switchId(s), true])))}
        />
      )}
    </section>
  );
}

/** Taking this machine back out of the work queues, in tiers: one click stops
 *  the work (every lane off, threads stop, nothing removed — and one click
 *  brings it back); the checkboxes compose the teardown command that removes
 *  what provisioning installed, run in the operator's own terminal like
 *  provisioning is, since it reaches brew and the VM. */
function UnprovisionSection(
  { anyOn, canStart, busy, onStop, onStart }: {
    anyOn: boolean; canStart: boolean; busy: string | null;
    onStop: () => void; onStart: () => void;
  },
) {
  const [more, setMore] = useState(false);
  const [parts, setParts] = useState<Record<string, boolean>>({});
  const [copied, setCopied] = useState(false);
  const command = [TEARDOWN, ...TEARDOWN_PARTS.filter((p) => parts[p.flag]).map((p) => `--${p.flag}`)].join(" ");

  return (
    <section className="setup-card">
      <h3>⏏️ Unprovision this computer</h3>
      {anyOn
        ? <>
            <p className="muted small">
              Stops running work here: every lane above goes off and the worker
              threads stop. Nothing installed is removed, so it comes back in one
              click.
            </p>
            <button disabled={busy != null} onClick={onStop}>stop running work on this computer</button>
          </>
        : <>
            <p className="muted small">
              This computer is not running work.
              {canStart && " Turn it back on in one click, or remove what provisioning installed below."}
            </p>
            {canStart && <button disabled={busy != null} onClick={onStart}>start running work again</button>}
          </>}
      <p>
        <button className="link-btn" onClick={() => setMore(!more)}>
          {more ? "▾" : "▸"} Also remove what provisioning installed…
        </button>
      </p>
      {more && (
        <div>
          {anyOn && (
            <p className="muted small">Stop running work first — the options stay off while a lane is on.</p>
          )}
          {TEARDOWN_PARTS.map((p) => (
            <div key={p.flag}>
              <label>
                <input type="checkbox" checked={!!parts[p.flag]} disabled={anyOn}
                  onChange={(e) => setParts({ ...parts, [p.flag]: e.target.checked })} />
                {" "}{p.label}
              </label>
              {" "}<span className="muted small">{p.hint}</span>
            </div>
          ))}
          <p>Run it in your own terminal, from the repo root:</p>
          <pre className="log-tail">{command}</pre>
          <button disabled={anyOn}
            onClick={() => { void navigator.clipboard.writeText(command); setCopied(true); }}>
            {copied ? "copied" : "copy command"}
          </button>
          <p className="muted small">
            With no option it only turns the lane switches off; each option above
            adds what it names. The script confirms each step (<code>--yes</code>
            {" "}skips the prompts), and the rows above go back to “needed” as it
            works through them.
          </p>
        </div>
      )}
    </section>
  );
}

type PushPath = "me" | "paste" | "dedicated";

/** Setting up the account autofix pushes as, on this machine. Three ways in —
 *  the operator's own account, a bundle pasted from a machine that has one,
 *  or a dedicated account — and every one ends the same way: GitHub names the
 *  account a key authenticates before the identity is written. */
function PushIdentitySection({ onDone }: { onDone: () => void }) {
  const [path, setPath] = useState<PushPath | null>(null);
  const options: { key: PushPath; label: string; hint: string }[] = [
    { key: "me", label: "Push fixes as me",
      hint: "fixes show up under your own GitHub account, through a new key made here just for Prospector" },
    { key: "paste", label: "Copy one from another computer",
      hint: "paste a share bundle from a computer that already has one (copied with “also let the teammate's machine push fixes” ticked)" },
    { key: "dedicated", label: "Use a separate account",
      hint: "a GitHub account you create just for this, so fixes show up under its name and its key reaches only this project" },
  ];
  return (
    <section className="setup-card">
      <h3>🔑 Contributor-push identity</h3>
      <p className="muted small">
        When a fix is approved, it is pushed to the contributor's branch under
        a GitHub account, using a key kept on this computer. Whose account
        should that be?
      </p>
      {options.map((o) => (
        <div key={o.key}>
          <label>
            <input type="radio" name="push-path" checked={path === o.key}
              onChange={() => setPath(o.key)} />
            {" "}{o.label}
          </label>
          {" "}<span className="muted small">{o.hint}</span>
        </div>
      ))}
      {path === "me" && <KeyFlow mode="me" onDone={onDone} />}
      {path === "dedicated" && <KeyFlow mode="dedicated" onDone={onDone} />}
      {path === "paste" && <PastePushIdentity onDone={onDone} />}
    </section>
  );
}

/** Generate (or name) a key, have the operator attach its public half to the
 *  account, and write the identity once GitHub greets that login. */
function KeyFlow({ mode, onDone }: { mode: "me" | "dedicated"; onDone: () => void }) {
  const [login, setLogin] = useState("");
  const [account, setAccount] = useState<PushAccount | null>(null);
  const [key, setKey] = useState<PushKeyInfo | null>(null);
  const [ownKey, setOwnKey] = useState(false);
  const [keyFile, setKeyFile] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (mode !== "me") return;
    let live = true;
    api.pushAccount().then((a) => { if (live) { setAccount(a); setLogin(a.login); } })
      .catch((e: unknown) => { if (live) setProblem(e instanceof Error ? e.message : String(e)); });
    return () => { live = false; };
  }, [mode]);

  const lookUp = async () => {
    setBusy("lookup"); setProblem(null);
    try {
      setAccount(await api.pushAccount(login.trim()));
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const generate = async () => {
    if (!account) return;
    setBusy("key"); setProblem(null);
    try {
      setKey(await api.pushKey(account.login));
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const check = async () => {
    if (!account) return;
    const file = ownKey ? keyFile.trim() : key?.path;
    if (!file) return;
    setBusy("probe"); setProblem(null);
    try {
      const probe = await api.pushProbe(account.login, ownKey ? file : undefined);
      if (!probe.ok) { setProblem(probe.problem ?? "GitHub did not confirm the key"); return; }
      await api.onboardingApply({
        step: "worker",
        env: { TRIAGE_PUSH_LOGIN: account.login, TRIAGE_PUSH_EMAIL: account.email,
          TRIAGE_PUSH_SSH_KEY_FILE: file },
      });
      onDone();
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const keysUrl = "https://github.com/settings/ssh/new";
  return (
    <div className="welcome-field">
      {mode === "me" && (
        <p className="muted small">
          Fixes land as you, and this key — passphrase-less, held on this
          machine — reaches every repository your account can push to. Fine for
          a computer you control; a dedicated account bounds it to this one.
        </p>
      )}
      {mode === "dedicated" && (
        <ol className="muted small">
          <li>Create the GitHub user (its own email; turn on 2FA, “Keep my email
            addresses private”, and “Block command line pushes that expose my email”).</li>
          <li>Add it to the repository as an <strong>outside collaborator with
            Write</strong> — maintainer edits unlock on push access to the base repo.</li>
          <li>Name it here, then generate a key below and add the public half to
            {" "}<em>that</em> account's SSH keys.</li>
        </ol>
      )}
      {mode === "dedicated" && (
        <div>
          <label htmlFor="push-login">Account login</label>{" "}
          <input id="push-login" value={login} placeholder="my-triage-pusher"
            onChange={(e) => { setLogin(e.target.value); setAccount(null); setKey(null); }} />
          {" "}
          <button className="btn-secondary" disabled={busy != null || login.trim() === ""}
            onClick={() => void lookUp()}>
            {busy === "lookup" ? "looking…" : "look up"}
          </button>
        </div>
      )}
      {account && (
        <p className="small">
          <strong>{account.login}</strong> · commits as <code>{account.email}</code>
        </p>
      )}
      {account && !ownKey && !key && (
        <button className="btn-primary" disabled={busy != null} onClick={() => void generate()}>
          {busy === "key" ? "generating…" : "generate a key on this machine"}
        </button>
      )}
      {account && key && !ownKey && (
        <>
          <p className="small">
            Add this public key to {mode === "me" ? "your" : <><code>{account.login}</code>'s</>} account
            at <a href={keysUrl} target="_blank" rel="noreferrer">github.com/settings/ssh/new</a>
            {mode === "dedicated" && " (signed in as that account)"}:
          </p>
          <pre className="log-tail">{key.public_key}</pre>
          <button className="btn-secondary"
            onClick={() => { void navigator.clipboard.writeText(key.public_key); setCopied(true); }}>
            {copied ? "copied" : "copy public key"}
          </button>
          <p className="muted small">Private half: <code>{key.path}</code>, owner-only, never leaves this machine unless you share it.</p>
        </>
      )}
      {account && (
        <p>
          <button className="link-btn" onClick={() => setOwnKey(!ownKey)}>
            {ownKey ? "▾" : "▸"} Use an existing passphrase-less key instead…
          </button>
        </p>
      )}
      {account && ownKey && (
        <div>
          <label htmlFor="push-key-file">Private key file</label>{" "}
          <input id="push-key-file" value={keyFile} placeholder="~/.ssh/prospector-push"
            onChange={(e) => setKeyFile(e.target.value)} />
        </div>
      )}
      {account && (key || (ownKey && keyFile.trim() !== "")) && (
        <div className="welcome-actions">
          <button className="btn-primary" disabled={busy != null} onClick={() => void check()}>
            {busy === "probe" ? "asking GitHub…" : "I added it — check and save"}
          </button>
          <span className="muted small">asks GitHub which account the key opens, then writes the identity</span>
        </div>
      )}
      {problem && <p className="chip chip-red sm">{problem}</p>}
    </div>
  );
}

/** A share bundle pasted on a configured machine: its contributor-push
 *  identity and the sharer's repository profile are taken; the repository and
 *  store are not. */
function PastePushIdentity({ onDone }: { onDone: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true); setProblem(null);
    try {
      await api.onboardingApply({ step: "worker", bundle: text });
      onDone();
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="welcome-field">
      <p className="muted small">
        On the other computer's Setup tab, under “Invite a member to this
        project”, tick “also let the teammate's machine push fixes” and copy.
        Paste it here: the push account and that computer's project settings
        (the fix permissions) are taken from it; nothing else changes.
      </p>
      <textarea className="welcome-paste" value={text} rows={8} disabled={busy}
        placeholder={'{\n  "version": 2,\n  "push": { "login": "…" }\n}'}
        onChange={(e) => setText(e.target.value)} />
      {problem && <p className="chip chip-red sm">{problem}</p>}
      <div className="welcome-actions">
        <button className="btn-primary" disabled={busy || text.trim() === ""} onClick={() => void submit()}>
          {busy ? "saving…" : "use this identity"}
        </button>
      </div>
    </div>
  );
}

/** A share bundle pasted on a configured machine whose push identity is
 *  already in place: only the sharer's repository profile is taken, which is
 *  where the agent-fix opt-in lives. Shown only while the agent-fix policy row
 *  is not met, since the push-identity card's paste covers the rest. */
function ProfileSection({ onDone }: { onDone: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true); setProblem(null);
    try {
      await api.onboardingApply({ step: "profile", bundle: text });
      onDone();
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="setup-card">
      <h3>📋 Fix permissions from another computer</h3>
      <p className="muted small">
        Before the AI may draft fixes without being asked, the project has to
        say what it is allowed to fix. A computer that already has that
        permission can hand it over: on its Setup tab, click “copy setup for a
        teammate” (no need to tick either box) and paste the result here. Only
        that permission list is taken from it; nothing else on this computer
        changes.
      </p>
      <textarea className="welcome-paste" value={text} rows={8} disabled={busy}
        placeholder={'{\n  "version": 2,\n  "profile": { … }\n}'}
        onChange={(e) => setText(e.target.value)} />
      {problem && <p className="chip chip-red sm">{problem}</p>}
      <div className="welcome-actions">
        <button className="btn-primary" disabled={busy || text.trim() === ""} onClick={() => void submit()}>
          {busy ? "saving…" : "use these permissions"}
        </button>
      </div>
    </section>
  );
}

/** Copies the deployment bundle a teammate pastes into their setup wizard. It
 *  carries the store URL, so it is a credential and the card says so. The
 *  bot's private key rides along only when the checkbox opts in. */
function ShareSection() {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const [includeKey, setIncludeKey] = useState(false);
  const [includePushKey, setIncludePushKey] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const copy = async () => {
    setProblem(null);
    try {
      const { bundle } = await api.setupShare(includeKey, includePushKey);
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
      <label className="small">
        <input type="checkbox" checked={includePushKey}
          onChange={(e) => setIncludePushKey(e.target.checked)} />
        {" "}Also let the teammate's machine push fixes — includes this machine's
        contributor-push identity and its SSH private key, so they can turn on
        autofix. Each copy widens where a credential with push to contributors'
        branches lives.
      </label>
      <p className="setup-warn small">
        ⚠ This carries the database password
        {includeKey && includePushKey ? ", the bot's private key, and the contributor-push SSH key"
          : includeKey ? " and the bot's private key"
          : includePushKey ? " and the contributor-push SSH key" : ""}.
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
