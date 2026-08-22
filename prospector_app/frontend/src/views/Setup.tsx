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
  push_identity: "The GitHub user account — yours or a dedicated one — whose SSH key pushes fixes to contributors' branches. Only autofix needs it.",
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
  // Artifacts on disk mean the machine was provisioned, whatever its switches
  // say now; the unprovision card lives in the worker section, so that stays.
  const provisioned = readiness.checks.some(
    (c) => (c.key === "sandbox_image" || c.key === "base_pin") && c.ok);

  return (
    <div className="pad">
      <h2>🛠️ Setup — {readiness.host}</h2>
      <p className="muted small">
        This page configures the machine serving it. Every machine that processes
        work is provisioned on its own; the Control tab reports the whole fleet.
      </p>

      {optedIn || expanded || provisioned
        ? <WorkerSection readiness={readiness} flags={flags} busy={busy} onToggle={toggle}
            onChanged={() => void load()} />
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
  const available = SWITCHES.filter(
    (s) => s.needs == null || byKey[s.needs] == null || byKey[s.needs].ok);
  const allOn = available.length > 0 && available.every((s) => flags[s.key] === "1");

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

      {byKey.push_identity != null && !byKey.push_identity.ok && (
        <PushIdentitySection onDone={onChanged} />
      )}

      <h4>What work should this machine do?</h4>
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

      {(optedIn || provisioned) && (
        <UnprovisionSection
          anyOn={SWITCHES.some((s) => flags[s.key] === "1")}
          canStart={available.length > 0}
          busy={busy}
          onStop={() => onToggle(Object.fromEntries(SWITCHES.map((s) => [s.key, false])))}
          onStart={() => onToggle(Object.fromEntries(available.map((s) => [s.key, true])))}
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
      hint: "fixes land under your own GitHub account, through a new key made here just for Prospector" },
    { key: "paste", label: "Paste one from another machine",
      hint: "a teammate's share bundle with “also let the teammate's machine push fixes” ticked" },
    { key: "dedicated", label: "Use a dedicated account",
      hint: "a separate GitHub user you create, so the key here reaches only this repository and fixes are attributed to it" },
  ];
  return (
    <section className="setup-card">
      <h3>🔑 Contributor-push identity</h3>
      <p className="muted small">
        Autofix pushes branch updates, rebases, and fixes to contributors'
        branches as a GitHub user account, through an SSH key this machine
        holds — only git pushes, never the API. Whose account?
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

/** A share bundle pasted on a configured machine: only its contributor-push
 *  identity is taken. */
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
        Your teammate ticks “also let the teammate's machine push fixes” under
        “Invite a member to this project” on their Setup tab. Only the push
        identity is taken from it here; the repository and store stay as they are.
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
