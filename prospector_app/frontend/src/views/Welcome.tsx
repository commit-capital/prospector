import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import {
  api,
  type OnboardingApplyBody,
  type OnboardingState,
  type ProbeFinding,
} from "../api";
import { useRepoMeta } from "../RepoMetaContext";

type Branch = "join" | "new" | null;
type Pick = "easy" | "full" | null;
type AgentPick = "claude" | "none" | null;

/** The onboarding state once the backend answers again, or null after ~10s of
 *  unreachability. */
async function stateWhenBack(): Promise<OnboardingState | null> {
  for (let tries = 0; tries < 20; tries++) {
    await new Promise((r) => setTimeout(r, 500));
    try {
      return await api.onboardingState();
    } catch {
      // still restarting
    }
  }
  return null;
}

/** Apply one onboarding step, riding out a dev-server restart: the step's
 *  `.env` write restarts Vite, which can sever the response of the very
 *  request that caused it after the backend has already adopted the write. A
 *  fetch that dies at the network level (TypeError) is therefore
 *  indeterminate — ask the server where it stands once it answers again, and
 *  let `landed` decide whether the step took. */
async function applyRidingRestart(
  body: OnboardingApplyBody,
  landed: (s: OnboardingState) => boolean,
): Promise<void> {
  try {
    await api.onboardingApply(body);
  } catch (e) {
    if (!(e instanceof TypeError)) throw e;
    const state = await stateWhenBack();
    if (!state || !landed(state)) throw e;
  }
}

export default function Welcome() {
  const [state, setState] = useState<OnboardingState | null>(null);
  const [branch, setBranch] = useState<Branch>(null);
  const [error, setError] = useState<string | null>(null);
  const { refresh } = useRepoMeta();

  // Re-read the shell's own metadata too: configuring the deployment renames
  // the app the user is looking at, and the topbar is built from it.
  const load = useCallback(() => {
    refresh();
    api.onboardingState().then(setState).catch((e: unknown) => {
      setError(e instanceof Error ? e.message : String(e));
    });
  }, [refresh]);

  // Deferred rather than called in the effect body, so the first render is not
  // also the first state write.
  useEffect(() => {
    const t = window.setTimeout(load, 0);
    return () => clearTimeout(t);
  }, [load]);

  // While the store snapshot is on its cold load the counts are not in yet;
  // poll until they are.
  useEffect(() => {
    if (!state?.loading) return;
    const t = window.setInterval(load, 2000);
    return () => window.clearInterval(t);
  }, [state?.loading, load]);

  if (error && !state) return <div className="pad"><p className="chip chip-red">{error}</p></div>;
  if (!state) return <div className="pad muted">reading this checkout…</div>;

  return (
    <div className="pad welcome">
      <h2>👋 Welcome to Prospector</h2>
      {state.configured
        ? <Ladder state={state} onChange={load} />
        : branch == null
          ? <BranchChoice onPick={setBranch} />
          : branch === "join"
            ? <JoinBranch onDone={load} onBack={() => setBranch(null)} />
            : <NewBranch onDone={load} onBack={() => setBranch(null)} />}
    </div>
  );
}

/** The first fork: someone already runs a deployment, or nobody does. */
function BranchChoice({ onPick }: { onPick: (b: Branch) => void }) {
  return (
    <>
      <p className="muted small">
        Prospector triages the open pull requests and issues of one repository.
        It needs to know which repository, and where to keep what it learns.
      </p>
      <div className="welcome-choice">
        <button className="welcome-card" onClick={() => onPick("join")}>
          <h3>🤝 Join a deployment</h3>
          <p className="muted small">
            A teammate sent you a setup bundle. Paste it and you are looking at
            the same triage data they are.
          </p>
        </button>
        <button className="welcome-card" onClick={() => onPick("new")}>
          <h3>🌱 Triage a new repository</h3>
          <p className="muted small">
            Point Prospector at a repository of your own. A few questions, each
            with an instant option and a thorough one.
          </p>
        </button>
      </div>
    </>
  );
}

function BackLink({ onBack }: { onBack: () => void }) {
  return (
    <button className="btn-secondary welcome-back" onClick={onBack}>
      ← start over
    </button>
  );
}

/** What a pasted bundle carries, read from its JSON so the page can say so
 *  before anything is written; null while the text is not a bundle yet. */
function describeBundle(text: string): { repo: string; store: boolean; profile: boolean; bot: string; key: boolean } | null {
  try {
    const doc: unknown = JSON.parse(text);
    if (typeof doc !== "object" || doc === null || !("env" in doc)) return null;
    const env = (doc as { env: unknown }).env;
    if (typeof env !== "object" || env === null) return null;
    const e = env as Record<string, unknown>;
    const pem = (doc as { bot_key_pem?: unknown }).bot_key_pem;
    return {
      repo: typeof e.TRIAGE_REPO === "string" ? e.TRIAGE_REPO : "",
      store: typeof e.TRIAGE_STORE_URL === "string" && e.TRIAGE_STORE_URL !== "",
      profile: "profile" in doc && (doc as { profile: unknown }).profile != null,
      bot: typeof e.TRIAGE_BOT_LOGIN === "string" ? e.TRIAGE_BOT_LOGIN : "",
      key: typeof pem === "string" && pem.trim() !== "",
    };
  } catch {
    return null;
  }
}

function JoinBranch({ onDone, onBack }: { onDone: () => void; onBack: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const carries = describeBundle(text);

  const submit = async () => {
    setBusy(true);
    setProblem(null);
    try {
      await applyRidingRestart({ step: "join", bundle: text }, (s) => s.configured);
      onDone();
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="setup-card">
      <h3>Paste the setup bundle</h3>
      <p className="muted small">
        Your teammate copies it from their Setup tab, under “Share this
        deployment”. It carries a database password, so it should have reached
        you privately — not through a channel with history.
      </p>
      <textarea className="welcome-paste" value={text} rows={10} disabled={busy}
        placeholder={'{\n  "version": 1,\n  "env": { "TRIAGE_REPO": "…" }\n}'}
        onChange={(e) => setText(e.target.value)} />
      {busy && (
        <p className="chip chip-blue sm" role="status">
          ⏳ Connecting — pointing this checkout at the repository and its store…
        </p>
      )}
      {carries && (
        <p className="muted small">
          This bundle carries: {carries.repo ? <><strong>{carries.repo}</strong></> : "a repository"}
          {carries.store ? " · a shared store" : ""}
          {carries.profile ? " · its repository profile" : ""}
          {carries.bot ? <> · the bot identity <code>{carries.bot}</code></> : ""}
          {carries.key && (
            <> · <strong>🔑 the bot's private key</strong> — approved actions will
              execute for real from this machine</>
          )}
        </p>
      )}
      {problem && <p className="chip chip-red sm">{problem}</p>}
      <div className="welcome-actions">
        <button disabled={busy || text.trim() === ""} onClick={() => void submit()}>
          {busy ? "connecting…" : "connect"}
        </button>
        <BackLink onBack={onBack} />
      </div>
    </section>
  );
}

/** One decision, with what the instant option gets you and what the thorough
 *  one costs. Neither is preselected — the choice is the point. */
function EasyOrFull(
  { title, easy, full, pick, onPick }: {
    title: string;
    easy: { label: string; detail: string };
    full: { label: string; detail: string };
    pick: Pick;
    onPick: (p: "easy" | "full") => void;
  },
) {
  return (
    <div className="welcome-fork">
      <h4>{title}</h4>
      {(["easy", "full"] as const).map((k) => {
        const o = k === "easy" ? easy : full;
        return (
          <label key={k} className={pick === k ? "fork-opt on" : "fork-opt"}>
            <input type="radio" checked={pick === k} onChange={() => onPick(k)} />
            {" "}<strong>{o.label}</strong>
            {" "}<span className="muted small">{o.detail}</span>
          </label>
        );
      })}
    </div>
  );
}

/** This machine's agent readiness (the local claude CLI), probed while
 *  `active`. */
function useAgentProbe(active: boolean): ProbeFinding | undefined {
  const [found, setFound] = useState<ProbeFinding | undefined>();
  useEffect(() => {
    if (!active) return;
    let stale = false;
    void api.onboardingProbe({ agent: true })
      .then((r) => { if (!stale) setFound(r.agent); })
      .catch(() => { if (!stale) setFound(undefined); });
    return () => { stale = true; };
  }, [active]);
  return found;
}

/** The agent-provider choice: who backs the “Ask the agent” sidebar. Probes
 *  this machine's claude CLI as soon as that option is picked. `title` is the
 *  fork heading; the ladder rung omits it, its card already carries one. */
function AgentChooser({ pick, onPick, title }: {
  pick: AgentPick; onPick: (p: "claude" | "none") => void; title?: string;
}) {
  const found = useAgentProbe(pick === "claude");
  return (
    <div className="welcome-fork">
      {title && <h4>{title}</h4>}
      <label className={pick === "claude" ? "fork-opt on" : "fork-opt"}>
        <input type="radio" checked={pick === "claude"} onChange={() => onPick("claude")} />
        {" "}<strong>Use your Claude account</strong>
        {" "}<span className="muted small">
          the “Ask the agent” sidebar runs the Claude Code CLI on this computer,
          under your own login.
        </span>
        {pick === "claude" && <>{" "}<Finding found={found} /></>}
        {pick === "claude" && found && !found.ok && (
          <p className="muted small">
            You can fix this later — the sidebar shows what to do.
          </p>
        )}
      </label>
      <label className="fork-opt" title="Not supported yet">
        <input type="radio" checked={false} disabled readOnly />
        {" "}<strong>Connect your Codex account</strong>
        {" "}<span className="muted small">not supported yet.</span>
      </label>
      <label className={pick === "none" ? "fork-opt on" : "fork-opt"}>
        <input type="radio" checked={pick === "none"} onChange={() => onPick("none")} />
        {" "}<strong>No agent support</strong>
        {" "}<span className="muted small">
          hides the sidebar; you can turn it on later from this page.
        </span>
      </label>
    </div>
  );
}

function Finding({ found }: { found: ProbeFinding | undefined }) {
  if (!found) return null;
  if (found.ok) {
    const counts = found.prs != null
      ? ` — ${found.prs} pull requests, ${found.clusters ?? 0} clusters`
      : "";
    return <span className="chip chip-green sm">looks good{counts}</span>;
  }
  return <span className="chip chip-red sm">{found.problem ?? "no"}</span>;
}

function NewBranch({ onDone, onBack }: { onDone: () => void; onBack: () => void }) {
  const [repo, setRepo] = useState("");
  const [repoFound, setRepoFound] = useState<ProbeFinding | undefined>();
  const [store, setStore] = useState<Pick>(null);
  const [storeUrl, setStoreUrl] = useState("");
  const [storeFound, setStoreFound] = useState<ProbeFinding | undefined>();
  const [app, setApp] = useState<Pick>(null);
  const [agent, setAgent] = useState<AgentPick>(null);
  const [botLogin, setBotLogin] = useState("");
  const [appId, setAppId] = useState("");
  const [keyFile, setKeyFile] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const ready = repo.includes("/") && store != null && app != null && agent != null
    && (store === "easy" || storeUrl.trim() !== "")
    && (app === "easy" || (botLogin.trim() !== "" && keyFile.trim() !== ""));

  const submit = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const env: Record<string, string> = { TRIAGE_REPO: repo.trim() };
      if (store === "full") env.TRIAGE_STORE_URL = storeUrl.trim();
      await applyRidingRestart({ step: "connect", env }, (s) => s.configured);
      if (app === "full") {
        await applyRidingRestart({
          step: "writes",
          env: {
            TRIAGE_BOT_LOGIN: botLogin.trim(),
            TRIAGE_BOT_APP_ID: appId.trim(),
            TRIAGE_BOT_KEY_FILE: keyFile.trim(),
          },
        }, (s) => s.bot_login === botLogin.trim());
      }
      if (agent != null) {
        await api.onboardingApply({
          step: "agent", env: { TRIAGE_AGENT_PROVIDER: agent },
        });
      }
      onDone();
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="setup-card">
      <h3>Triage a new repository</h3>

      <div className="welcome-field">
        <label htmlFor="repo">Which repository? <span className="muted small">owner/name</span></label>
        <input id="repo" value={repo} placeholder="octocat/hello-world"
          onChange={(e) => setRepo(e.target.value)}
          onBlur={() => {
            if (repo.trim())
              void api.onboardingProbe({ repo: repo.trim() })
                .then((r) => setRepoFound(r.repo)).catch(() => setRepoFound(undefined));
          }} />
        {" "}<Finding found={repoFound} />
        <p className="muted small">
          Read with your own GitHub login, so you need access to it — nothing is
          written to it until you say so.
        </p>
      </div>

      <EasyOrFull
        title="Where should triage data live?"
        easy={{
          label: "On this computer",
          detail: "nothing to set up, works immediately.",
        }}
        full={{
          label: "In a cloud database",
          detail: "so a team shares one store — needs a database like Supabase, about 5 minutes.",
        }}
        pick={store} onPick={setStore} />
      {store === "full" && (
        <div className="welcome-field">
          <label htmlFor="store">Database URL</label>
          <input id="store" value={storeUrl} placeholder="postgresql+psycopg://…"
            onChange={(e) => setStoreUrl(e.target.value)}
            onBlur={() => {
              if (storeUrl.trim())
                void api.onboardingProbe({ store_url: storeUrl.trim() })
                  .then((r) => setStoreFound(r.store)).catch(() => setStoreFound(undefined));
            }} />
          {" "}<Finding found={storeFound} />
        </div>
      )}

      <EasyOrFull
        title="Should a bot be able to act on the repository?"
        easy={{
          label: "Not yet",
          detail: "see and analyze it with your own GitHub login — no writes, nothing to create.",
        }}
        full={{
          label: "Set up a GitHub App",
          detail: "so a bot can merge, close, and comment for you — about 10 minutes on github.com.",
        }}
        pick={app} onPick={setApp} />
      {app === "full" && (
        <div className="welcome-field">
          <p className="muted small">
            Create the App on{" "}
            <a href="https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app"
              target="_blank" rel="noreferrer">github.com</a>, install it on the
            repository, then download its private key and point at the file.
          </p>
          <label htmlFor="bot">App login</label>
          <input id="bot" value={botLogin} placeholder="my-triage-bot"
            onChange={(e) => setBotLogin(e.target.value)} />
          <label htmlFor="appid">App ID</label>
          <input id="appid" value={appId} placeholder="12345"
            onChange={(e) => setAppId(e.target.value)} />
          <label htmlFor="key">Private key file</label>
          <input id="key" value={keyFile} placeholder="~/.config/my-app/private-key.pem"
            onChange={(e) => setKeyFile(e.target.value)} />
        </div>
      )}

      <AgentChooser pick={agent} onPick={setAgent} title="In-app agent support" />

      {problem && <p className="chip chip-red sm">{problem}</p>}
      <div className="welcome-actions">
        <button disabled={!ready || busy} onClick={() => void submit()}>
          {busy ? "setting up…" : "set up Prospector"}
        </button>
        <BackLink onBack={onBack} />
      </div>
    </section>
  );
}

/** The three rungs. A rung already satisfied renders as done, so a reload
 *  mid-setup resumes rather than asking again. */
function Ladder({ state, onChange }: { state: OnboardingState; onChange: () => void }) {
  const name = state.display_name || state.repo;
  return (
    <>
      {state.loading
        ? <section className="setup-card">
            <h3>⏳ Connected to {name} — loading its triage data…</h3>
            <p className="muted small" role="status">
              Reading every pull request and cluster from the store into memory.
              A large deployment can take a minute; the steps below work meanwhile.
            </p>
          </section>
        : <section className="setup-card setup-done">
            <h3>✅ You can see {name}</h3>
            <p className="muted small">
              {state.counts.prs ?? 0} pull requests and {state.counts.clusters ?? 0}{" "}
              clusters loaded from the store. <Link to="/">Go look around</Link> — or
              keep going below.
            </p>
          </section>}

      {state.writes_ready
        ? <section className="setup-card setup-done">
            <h3>✅ You can write to {name}</h3>
            <p className="muted small">
              Approved actions post as <code>{state.bot_login}</code>.
            </p>
          </section>
        : <WritesStep name={name} onDone={onChange} />}

      <AgentStep state={state} onDone={onChange} />

      {state.worker_ready
        ? <section className="setup-card setup-done">
            <h3>✅ This computer runs automated tasks</h3>
            <p className="muted small">
              Manage its lanes on the <Link to="/setup">Setup tab</Link>.
            </p>
          </section>
        : <section className="setup-card">
            <h3>Optional last step: run automated tasks here</h3>
            <p className="muted small">
              Analyze, test, and fix pull requests in a sandbox on this machine.
              Heavier, and meant for a computer you can leave running.
            </p>
            <Link to="/setup?provision=1">Set this computer up →</Link>
          </section>}
    </>
  );
}

/** The agent rung: pick who backs the “Ask the agent” sidebar, or show the
 *  standing choice with this machine's readiness. */
function AgentStep({ state, onDone }: { state: OnboardingState; onDone: () => void }) {
  const chosen: AgentPick =
    state.agent_provider === "claude" || state.agent_provider === "none"
      ? state.agent_provider : null;
  const [pick, setPick] = useState<AgentPick>(chosen);
  const [editing, setEditing] = useState(chosen == null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const found = useAgentProbe(!editing && chosen === "claude");

  const apply = async () => {
    if (pick == null) return;
    setBusy(true);
    setProblem(null);
    try {
      await api.onboardingApply({ step: "agent", env: { TRIAGE_AGENT_PROVIDER: pick } });
      setEditing(false);
      onDone();
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!editing && chosen === "claude") {
    return (
      <section className="setup-card setup-done">
        <h3>✅ The “Ask the agent” sidebar uses your Claude account</h3>
        <p className="muted small">
          It runs the Claude Code CLI on this computer, under your own login.
          {" "}<Finding found={found} />
          {found && !found.ok && found.problem === "not logged in" && (
            <> Run <code>claude auth login</code> in a terminal to finish.</>
          )}
          {" "}<button className="link-btn" onClick={() => setEditing(true)}>change</button>
        </p>
      </section>
    );
  }
  if (!editing && chosen === "none") {
    return (
      <section className="setup-card setup-done">
        <h3>✅ In-app agent support is off</h3>
        <p className="muted small">
          The “Ask the agent” sidebar is hidden.
          {" "}<button className="link-btn" onClick={() => setEditing(true)}>change</button>
        </p>
      </section>
    );
  }
  return (
    <section className="setup-card">
      <h3>Optional: in-app agent support</h3>
      <p className="muted small">
        The “Ask the agent” sidebar answers questions about the app, a PR, or
        the codebase. It needs an AI account on this computer.
      </p>
      <AgentChooser pick={pick} onPick={setPick} />
      {problem && <p className="chip chip-red sm">{problem}</p>}
      <div className="welcome-actions">
        <button disabled={busy || pick == null} onClick={() => void apply()}>
          {busy ? "saving…" : "save"}
        </button>
        {chosen != null && (
          <button className="btn-secondary" onClick={() => { setPick(chosen); setEditing(false); }}>
            cancel
          </button>
        )}
      </div>
    </section>
  );
}

function WritesStep({ name, onDone }: { name: string; onDone: () => void }) {
  const [botLogin, setBotLogin] = useState("");
  const [appId, setAppId] = useState("");
  const [keyFile, setKeyFile] = useState("");
  const [keyFound, setKeyFound] = useState<ProbeFinding | undefined>();
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setProblem(null);
    const body: OnboardingApplyBody = {
      step: "writes",
      env: {
        TRIAGE_BOT_LOGIN: botLogin.trim(),
        TRIAGE_BOT_APP_ID: appId.trim(),
        TRIAGE_BOT_KEY_FILE: keyFile.trim(),
      },
    };
    try {
      await applyRidingRestart(body, (s) => s.bot_login === botLogin.trim());
      onDone();
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="setup-card">
      <h3>Next step: let Prospector write to {name}</h3>
      <p className="muted small">
        Merging, closing, and commenting on your behalf go out as a GitHub App
        you own, so every action is attributable to it rather than to you.
        Create it on{" "}
        <a href="https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app"
          target="_blank" rel="noreferrer">github.com</a>, install it on the
        repository, and point at its private key below. Until then Prospector
        reads {name} and previews every action without performing it.
      </p>
      <div className="welcome-field">
        <label htmlFor="w-bot">App login</label>
        <input id="w-bot" value={botLogin} placeholder="my-triage-bot"
          onChange={(e) => setBotLogin(e.target.value)} />
        <label htmlFor="w-appid">App ID</label>
        <input id="w-appid" value={appId} placeholder="12345"
          onChange={(e) => setAppId(e.target.value)} />
        <label htmlFor="w-key">Private key file</label>
        <input id="w-key" value={keyFile} placeholder="~/.config/my-app/private-key.pem"
          onChange={(e) => setKeyFile(e.target.value)}
          onBlur={() => {
            if (keyFile.trim())
              void api.onboardingProbe({ key_file: keyFile.trim() })
                .then((r) => setKeyFound(r.key_file)).catch(() => setKeyFound(undefined));
          }} />
        {" "}<Finding found={keyFound} />
      </div>
      {problem && <p className="chip chip-red sm">{problem}</p>}
      <div className="welcome-actions">
        <button
          disabled={busy || botLogin.trim() === "" || keyFile.trim() === ""}
          onClick={() => void submit()}>
          {busy ? "checking…" : "enable writes"}
        </button>
      </div>
    </section>
  );
}
