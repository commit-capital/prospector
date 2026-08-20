import { Component, lazy, Suspense, useEffect, useState, type ErrorInfo, type ReactNode } from "react";
import { NavLink, Outlet, useLocation, useSearchParams } from "react-router";
import { ExecProvider, useExec, type Toast } from "./ExecContext";
import { RepoMetaProvider, useRepoMeta } from "./RepoMetaContext";
import { FeedbackButton } from "./components/FeedbackButton";
import { AgentPaneProvider } from "./components/AgentPane";
import { isReachable, subscribeHealth, pingHealth } from "./health";
import { api } from "./api";
import { loadWithRecovery } from "./lazyLoad";
import { timeAgo } from "./timeAgo";

const PRFlyout = lazy(() => loadWithRecovery("pr-flyout", async () => ({
  default: (await import("./components/PRFlyout")).PRFlyout,
})));
const IssueFlyout = lazy(() => loadWithRecovery("issue-flyout", async () => ({
  default: (await import("./components/IssueFlyout")).IssueFlyout,
})));

type FlyoutLoadBoundaryProps = {
  children: ReactNode;
  onDismiss: () => void;
};

type FlyoutLoadBoundaryState = {
  error: Error | null;
};

class FlyoutLoadBoundary extends Component<FlyoutLoadBoundaryProps, FlyoutLoadBoundaryState> {
  state: FlyoutLoadBoundaryState = { error: null };

  static getDerivedStateFromError(error: unknown): FlyoutLoadBoundaryState {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Flyout module failed to load", error, info.componentStack);
  }

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <>
        <div className="flyout-scrim" onClick={this.props.onDismiss} />
        <aside className="flyout flyout-load-error" role="alert" aria-label="Details failed to load">
          <h2>Couldn&rsquo;t open details</h2>
          <p>This part of the cockpit didn&rsquo;t load. Reload the page to try again.</p>
          <div className="flyout-load-actions">
            <button className="btn-primary" onClick={() => window.location.reload()}>Reload</button>
            <button className="btn-secondary" onClick={this.props.onDismiss}>Dismiss</button>
          </div>
          <details>
            <summary>Technical details</summary>
            <code>{this.state.error.message}</code>
          </details>
        </aside>
      </>
    );
  }
}

function Flyouts() {
  const [params, setParams] = useSearchParams();
  const flyoutKey = `${params.get("pr") ?? ""}:${params.get("issue") ?? ""}`;
  const dismiss = (): void => {
    const next = new URLSearchParams(params);
    next.delete("pr");
    next.delete("issue");
    setParams(next, { replace: true });
  };
  return (
    <FlyoutLoadBoundary key={flyoutKey} onDismiss={dismiss}>
      <Suspense fallback={null}>
        {params.has("pr") && <PRFlyout />}
        {params.has("issue") && <IssueFlyout />}
      </Suspense>
    </FlyoutLoadBoundary>
  );
}

// Maps the active route to the nav label shown in the tab title, longest-prefix
// first so nested routes (e.g. /explore/123) resolve to their parent view.
const VIEW_NAMES: [string, string][] = [
  ["/explore", "PR Explorer"],
  ["/differ", "PR Differ"],
  ["/issues", "Issues"],
  ["/alerts", "Alerts"],
  ["/action-items", "Action Items"],
  ["/control", "Control"],
  ["/setup", "Setup"],
  ["/activity", "Activity"],
  ["/tables", "Tables"],
  ["/clusters", "Clusters"],
  ["/", "Home"],
];

function viewName(pathname: string): string {
  const cluster = pathname.match(/^\/clusters\/([^/]+)/);
  if (cluster) return `Cluster ${cluster[1]}`;
  return VIEW_NAMES.find(([prefix]) => pathname === prefix || pathname.startsWith(prefix + "/"))?.[1]
    ?? VIEW_NAMES[VIEW_NAMES.length - 1][1];
}

// Labels which checkout this app is serving — the git branch and worktree
// dir the backend runs from — so multiple instances (one per worktree) are
// tellable apart. Renders a full-width bar at the top of the page and sets the
// tab title. The title leads with the worktree name (short, single-segment) so
// it stays legible when the browser truncates a narrow tab; the bar shows the
// full branch. On `main` there's nothing to disambiguate, so it stays hidden and
// the title is just the base name.
function InstanceBar() {
  const [inst, setInst] = useState<{ branch: string | null; worktree: string | null } | null>(null);
  const { meta } = useRepoMeta();
  useEffect(() => {
    api.instance().then(setInst).catch(() => {});
  }, []);
  const onMain = inst?.branch === "main";
  const { pathname } = useLocation();
  useEffect(() => {
    const lead = onMain ? null : inst?.worktree || inst?.branch;
    const base = `${meta ? `${meta.display_name} Prospector` : "Prospector"} | ${viewName(pathname)}`;
    document.title = lead ? `${lead} · ${base}` : base;
  }, [inst, onMain, pathname, meta]);

  if (onMain || (!inst?.branch && !inst?.worktree)) return null;
  // The worktree dir is usually the branch's trailing segment (a `prefix/leaf`
  // branch checked out into a `leaf/` dir). Only surface it when it actually
  // differs — otherwise it's redundant with the branch already shown.
  const branchLeaf = inst.branch?.split("/").pop();
  const showWt = inst.worktree && inst.worktree !== inst.branch && inst.worktree !== branchLeaf;
  return (
    <div className="instance-bar">
      <span className="instance-bar-icon">⎇</span>
      {inst.branch && <span className="instance-bar-branch">{inst.branch}</span>}
      {showWt && <span className="instance-bar-wt">worktree <code>{inst.worktree}</code></span>}
    </div>
  );
}

// The topbar product name, from backend repo metadata (display name defaults to
// the configured repo's short name).
function Brand() {
  const { meta } = useRepoMeta();
  return <div className="brand">⛏️ {meta ? `${meta.display_name} Prospector` : "Prospector"}</div>;
}

function ScrollToTop() {
  const { pathname } = useLocation();
  useEffect(() => { window.scrollTo(0, 0); }, [pathname]);
  return null;
}

function StoreWriteBanner() {
  const { storeWriteBlock } = useExec();
  if (!storeWriteBlock) return null;
  return <div className="store-write-block" role="alert">⛔ {storeWriteBlock}</div>;
}

function IdentityPicker() {
  const { identities, botLogin, identity, setIdentity, dryRun, setDryRun, livePossible,
    liveError, storeWriteBlock, retryLive, pushToast } = useExec();
  const [retrying, setRetrying] = useState(false);
  // Whether this machine can go live is probed once by the backend and cached
  // for its whole process lifetime — a key file added, an app installed, or a
  // network blip cleared after that first probe otherwise never takes effect
  // without a restart. This forces a re-probe on demand.
  const retry = async () => {
    setRetrying(true);
    try {
      const ok = await retryLive();
      pushToast(ok ? `${botLogin} token minted — live mode is available.` : `Still no ${botLogin} token.`,
        ok ? "green" : "yellow", ok ? undefined : { detail: liveError || undefined });
    } finally {
      setRetrying(false);
    }
  };
  const dryRunTitle = storeWriteBlock
    ? storeWriteBlock
    : livePossible
    ? "Toggle dry-run / live posting"
    : `No ${botLogin} token on this machine — dry-run only${liveError ? ` (${liveError})` : ""}`;
  return (
    <div className="identity-picker">
      <span className="muted small">Posting as</span>
      <select value={identity} onChange={(e) => setIdentity(e.target.value)} disabled={identities.length <= 1}>
        {identities.map((i) => <option key={i.id} value={i.id}>{i.label}</option>)}
      </select>
      <button
        className={`mode-badge ${dryRun ? "dry" : "live"}`}
        onClick={() => setDryRun(!dryRun)}
        disabled={!livePossible || !!storeWriteBlock}
        title={dryRunTitle}
      >
        {dryRun ? "DRY RUN" : "● LIVE"}
      </button>
      {!livePossible && (
        <button className="retry-live-btn" onClick={retry} disabled={retrying}
          title={`Re-check for a ${botLogin} token now, instead of restarting the backend.${liveError ? ` Last reason: ${liveError}` : ""}`}>
          {retrying ? "↻ checking…" : "↻ retry"}
        </button>
      )}
    </div>
  );
}

// "live · 2m" — when this machine last pulled live PR state (open/closed/merged)
// from GitHub. Refreshes on launch (background sweep); click to re-pull now.
function LiveStatus() {
  const [at, setAt] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = () => api.liveStatus().then((d) => setAt(d.fetched_at)).catch(() => {});
  useEffect(() => {
    load();
    const t = setTimeout(load, 6000); // the launch sweep lands a few seconds in
    return () => clearTimeout(t);
  }, []);
  const refresh = async () => {
    setBusy(true);
    try { const r = await api.refreshLive(); setAt(r.fetched_at); } finally { setBusy(false); }
  };
  return (
    <button className="live-status" onClick={refresh} disabled={busy}
      title="Live PR state (open / closed / merged) fetched from GitHub — the same view every operator sees. Click to refresh now.">
      {busy ? "↻ syncing…" : `live · ${at ? timeAgo(at) : "—"}`}
    </button>
  );
}

const TOAST_MS = 8000;

// One toast. Auto-dismisses after TOAST_MS, but the timer pauses while the
// cursor is over it (so it never vanishes mid-read) and restarts on leave.
// Clicking dismisses immediately.
function ToastItem({ t }: { t: Toast }) {
  const { dismissToast } = useExec();
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    if (paused) return;
    const h = window.setTimeout(() => dismissToast(t.id), TOAST_MS);
    return () => window.clearTimeout(h);
  }, [paused, t.id, dismissToast]);
  return (
    <div className={`toast toast-${t.tone}`} onClick={() => dismissToast(t.id)} title="dismiss"
      onMouseEnter={() => setPaused(true)} onMouseLeave={() => setPaused(false)}>
      <div className="toast-body">
        <div className="toast-title">{t.title}</div>
        {t.effects && (
          <ul className="toast-effects">
            {t.effects.map((e, i) => (
              <li key={i}><span className="toast-eff-label">{e.label}</span> — {e.value}</li>
            ))}
          </ul>
        )}
        {!t.effects && t.detail && <div className="toast-detail">{t.detail}</div>}
      </div>
      <span className="toast-x">✕</span>
    </div>
  );
}

function Toasts() {
  const { toasts } = useExec();
  if (!toasts.length) return null;
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => <ToastItem key={t.id} t={t} />)}
    </div>
  );
}

function ThemeToggle() {
  const [theme, setTheme] = useState(document.documentElement.dataset.theme || "light");
  const flip = () => {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("app-theme", next);
    setTheme(next);
  };
  return (
    <button className="theme-toggle" onClick={flip} title="Toggle light/dark">
      {theme === "dark" ? "☀️" : "🌙"}
    </button>
  );
}

// Loud, dismissable-by-recovery banner shown whenever the backend API can't be
// reached. Polls /api/health (faster while down) so the page heals itself once
// the backend is back up, without a manual refresh.
function BackendBanner() {
  const [reachable, setReachable] = useState(isReachable());
  useEffect(() => {
    const unsub = subscribeHealth(setReachable);
    let timer: number;
    const tick = async () => {
      await pingHealth();
      timer = window.setTimeout(tick, isReachable() ? 15000 : 3000);
    };
    tick();
    return () => { unsub(); window.clearTimeout(timer); };
  }, []);

  if (reachable) return null;
  return (
    <div className="backend-down" role="alert">
      ⚠ Can't reach the backend API on <code>:{__API_PORT__}</code>. Is{" "}
      <code>prospector serve --dev</code> running? This page will recover on its own once it's back.
    </div>
  );
}

export default function App() {
  // Expose the topbar's (wrap-variable) height as a CSS var so page-scrolled
  // sticky headers — like the cluster page's diff grid — sit just beneath it.
  useEffect(() => {
    const tb = document.querySelector<HTMLElement>(".topbar");
    if (!tb || typeof ResizeObserver === "undefined") return;
    const set = () => document.documentElement.style.setProperty("--topbar-h", `${tb.offsetHeight}px`);
    set();
    const ro = new ResizeObserver(set);
    ro.observe(tb);
    return () => ro.disconnect();
  }, []);
  return (
    <RepoMetaProvider>
    <ExecProvider>
      <AgentPaneProvider>
      <ScrollToTop />
      <div className="app">
        <BackendBanner />
        <StoreWriteBanner />
        <header className="topbar">
          <Brand />
          <nav>
            <NavLink to="/" end>🏠 Home</NavLink>
            <NavLink to="/clusters">Clusters</NavLink>
            <NavLink to="/explore">🔭 PR Explorer</NavLink>
            <NavLink to="/differ">🔬 PR Differ</NavLink>
            <NavLink to="/issues">🐛 Issues</NavLink>
            <NavLink to="/alerts">🛡️ Alerts</NavLink>
            <NavLink to="/action-items">🗂️ Action Items</NavLink>
            <NavLink to="/control">🎛️ Control</NavLink>
            <NavLink to="/setup">🛠️ Setup</NavLink>
            <NavLink to="/activity">📋 Activity</NavLink>
            <NavLink to="/tables">🗄️ Tables</NavLink>
          </nav>
          <div className="topbar-right">
            <LiveStatus />
            <IdentityPicker />
            <FeedbackButton />
            <ThemeToggle />
          </div>
        </header>
        <main className="content">
          <InstanceBar />
          <Outlet />
        </main>
        <Toasts />
        <Flyouts />
      </div>
      </AgentPaneProvider>
    </ExecProvider>
    </RepoMetaProvider>
  );
}
