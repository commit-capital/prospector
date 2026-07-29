import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, type Identity, type ExecResult, type IssueExecResult, type ReviewCap } from "./api";

// The no-provider default until /api/capabilities resolves (and when the backend
// runs with no external review provider). Hides all Greptile UI.
const NO_REVIEW: ReviewCap = {
  provider: "none", label: "", threshold: null, score_max: null,
  retrigger: false, stale_tracking: false,
};

export interface Effect { label: string; value: string }
export interface Toast { id: number; title: string; detail?: string; effects?: Effect[]; tone: "green" | "red" | "yellow" | "muted" }

interface ExecState {
  identities: Identity[];
  botLogin: string;
  identity: string;
  setIdentity: (s: string) => void;
  dryRun: boolean;
  setDryRun: (b: boolean) => void;
  livePossible: boolean;
  // Why live mode isn't available (key missing, bad app id, no installation, …)
  // — from get-bot-token.sh's stderr, surfaced instead of silently discarded.
  liveError: string | null;
  // Re-probes live_possible (see api.refreshIdentities) instead of waiting for
  // a backend restart — the probe is cached for the process's lifetime, so a
  // fix made after the first probe otherwise never takes effect. Resolves to
  // whether it's live-possible now, so a caller can react (e.g. a toast).
  retryLive: () => Promise<boolean>;
  canMergeUpstream: boolean;
  login: string | null;
  // The configured external review provider (Greptile / none). UI hides its
  // Greptile column, filter, detail card, and retrigger when provider is "none".
  review: ReviewCap;
  // global action feedback + refresh signal (#67/#69). Any action site calls
  // reportResult(res) to surface a toast and, if something actually landed,
  // bump actionTick so run-state badges across the app refetch.
  toasts: Toast[];
  pushToast: (title: string, tone?: Toast["tone"], opts?: { detail?: string; effects?: Effect[] }) => void;
  dismissToast: (id: number) => void;
  actionTick: number;
  reportResult: (res: ExecResult | IssueExecResult) => void;
}

const Ctx = createContext<ExecState>({
  identities: [], botLogin: "bot", identity: "", setIdentity: () => {},
  dryRun: true, setDryRun: () => {}, livePossible: false,
  liveError: null, retryLive: async () => false,
  canMergeUpstream: false, login: null, review: NO_REVIEW,
  toasts: [], pushToast: () => {}, dismissToast: () => {}, actionTick: 0, reportResult: () => {},
});

// eslint-disable-next-line react-refresh/only-export-components -- context hook co-located with its provider
export const useExec = () => useContext(Ctx);

const LANDED = new Set(["executed", "merged", "reopened"]);
const TONE_FOR: Record<string, Toast["tone"]> = {
  executed: "green", merged: "green", reopened: "green",
  error: "red", blocked: "red", skipped: "muted", "dry-run": "yellow",
};

// A short, human action name for the toast title (the executor's raw action is
// e.g. "REVIEW:request-changes" / "CLOSE_DUP").
function friendlyAction(action: string): string {
  if (action.startsWith("REVIEW:")) {
    const ev = action.slice("REVIEW:".length);
    return ev === "request-changes" ? "request changes" : ev; // approve | comment
  }
  const map: Record<string, string> = {
    CLOSE: "close", CLOSE_DUP: "close (dup)", CLOSE_FIXED: "close (already-fixed)",
    CLOSE_STALE: "close (stale)", MERGE: "merge", GREPTILE_RETRIGGER: "Greptile re-trigger",
  };
  return map[action] ?? action.toLowerCase().replace(/_/g, " ");
}

// What the action does to each surface, as labeled bullets — rendered instead
// of the executor's prose detail when we know the action's shape. Null falls
// back to the prose detail.
function effectsFor(action: string): Effect[] | null {
  // The triage store is never written directly by an action. Review/line-comment
  // leave the PR's upstream state alone, so the store stays "unmodified". Close/
  // merge/reopen change upstream state, so the store reflects it on the next
  // INGEST — say so rather than imply the record is frozen.
  const STORE_FROZEN: Effect = { label: "Store", value: "unmodified" };
  const STORE_INGEST: Effect = { label: "Store", value: "updates on next ingest" };
  if (action.startsWith("REVIEW:")) {
    const ev = action.slice("REVIEW:".length);
    return [
      { label: "PR", value: "stays open" },
      STORE_FROZEN,
      { label: ev === "approve" ? "Review" : "Comment", value: ev === "approve" ? "approved" : "added" },
    ];
  }
  if (action === "LINE_COMMENT")
    return [{ label: "PR", value: "unchanged" }, { label: "Comment", value: "added (inline)" }, STORE_FROZEN];
  if (action === "REOPEN")
    return [{ label: "PR", value: "reopened" }, { label: "Bot comment", value: "removed" },
            { label: "Change request", value: "withdrawn (if any)" }, STORE_INGEST];
  if (action === "MERGE")
    return [{ label: "PR", value: "merged (squash)" }, STORE_INGEST];
  if (action.startsWith("CLOSE"))
    return [{ label: action.includes("ISSUE") ? "Issue" : "PR", value: "closed (reopenable)" },
            { label: "Comment", value: "added" }, STORE_INGEST];
  return null;
}

export function ExecProvider({ children }: { children: ReactNode }) {
  const [identities, setIdentities] = useState<Identity[]>([]);
  const [identity, setIdentity] = useState("");
  const [livePossible, setLivePossible] = useState(false);
  const [liveError, setLiveError] = useState<string | null>(null);
  // Persist dry-run/live per-tab so a full reload (e.g. a manual address-bar edit,
  // which tears down this provider) keeps the mode. sessionStorage not localStorage:
  // a reopened tab resets to dry-run, so live can't silently linger. "0" = live.
  const [dryRun, setDryRun] = useState(() => {
    try { return sessionStorage.getItem("cockpit-dry-run") !== "0"; } catch { return true; }
  });
  useEffect(() => {
    try { sessionStorage.setItem("cockpit-dry-run", dryRun ? "1" : "0"); } catch { /* storage unavailable */ }
  }, [dryRun]);
  const [canMergeUpstream, setCanMerge] = useState(false);
  const [login, setLogin] = useState<string | null>(null);
  const [review, setReview] = useState<ReviewCap>(NO_REVIEW);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [actionTick, setActionTick] = useState(0);
  const nextId = useRef(1);

  useEffect(() => {
    api.identities().then((d) => {
      setIdentities(d.identities);
      // Default the active identity to the backend's first (the bot) unless the
      // operator already picked one — the bot login lives in settings, not here.
      setIdentity((prev) => prev || d.identities[0]?.id || "");
      setLivePossible(d.live_possible);
      setLiveError(d.live_error);
      if (!d.live_possible) setDryRun(true);
    }).catch(() => {});
    api.capabilities().then((c) => {
      setCanMerge(c.merge_upstream); setLogin(c.login); setReview(c.review ?? NO_REVIEW);
    }).catch(() => {});
  }, []);

  // "Retry live mode": the backend only probes whether it can mint a bot token
  // once and caches the result for its whole process
  // lifetime (executor.live_possible), so a key file added, an app installed,
  // or a transient network failure cleared after that first probe otherwise
  // never takes effect without restarting the backend. This re-probes on
  // demand and applies the fresh result, same shape as the mount effect.
  // /api/capabilities is best-effort here (mirrors the mount effect's own
  // .catch(() => {})) — a login-lookup hiccup must not swallow the identities
  // refresh outcome the operator is actually waiting on.
  const retryLive = useCallback(async (): Promise<boolean> => {
    const d = await api.refreshIdentities();
    setIdentities(d.identities);
    setLivePossible(d.live_possible);
    setLiveError(d.live_error);
    if (!d.live_possible) setDryRun(true);
    await api.capabilities().then((c) => {
      setCanMerge(c.merge_upstream); setLogin(c.login); setReview(c.review ?? NO_REVIEW);
    }).catch(() => {});
    return d.live_possible;
  }, []);

  const dismissToast = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);
  const pushToast = useCallback((title: string, tone: Toast["tone"] = "muted",
                                 opts?: { detail?: string; effects?: Effect[] }) => {
    const id = nextId.current++;
    // No auto-dismiss timer here — each rendered toast owns its own timer so it
    // can pause while the cursor is over it (see <Toasts> in App).
    setToasts((t) => [...t.slice(-4), { id, title, detail: opts?.detail, effects: opts?.effects, tone }]);
  }, []);

  const reportResult = useCallback((res: ExecResult | IssueExecResult) => {
    const tone = TONE_FOR[res.status] ?? "muted";
    const dry = res.status === "dry-run";
    // Dry-run is the headline (it's what the reader most needs to know), so it
    // leads the title; "would"/"nothing posted" are redundant once it's there.
    const prefix = dry ? (res.forced ? "(dry run · no token) " : "(dry run) ") : "";
    // Only annotate the title with the raw status when it's not the obvious
    // success/preview path (error, skipped, …).
    const suffix = dry || res.status === "executed" || res.status === "merged"
      || res.status === "reopened" ? "" : ` · ${res.status}`;
    const id = "pr" in res ? res.pr : res.issue;
    const title = `${prefix}#${id} · ${friendlyAction(res.action)}${suffix}`;
    // Structured effects only when the action actually previews or lands
    // (dry-run / executed / merged / reopened) — a skipped or errored action
    // describes itself in prose, so never show success bullets for it.
    const effects = dry || LANDED.has(res.status) ? effectsFor(res.action) : null;
    pushToast(title, tone, effects ? { effects } : { detail: res.detail || undefined });
    if (LANDED.has(res.status)) setActionTick((n) => n + 1); // landed → refresh badges everywhere
  }, [pushToast]);

  // Toggling dry-run resets transient per-action state across the app (#67):
  // bump the tick so run-state/badges refetch and components clear stale chips,
  // and announce the mode change so it's never ambiguous which mode you're in.
  const setDryRunGuarded = useCallback((b: boolean) => {
    const next = livePossible ? b : true;
    setDryRun(next);
    setActionTick((n) => n + 1);
    pushToast(
      next ? "Dry-run mode — actions only preview; nothing is posted."
           : `● LIVE mode — actions now post upstream as ${identities[0]?.id ?? "the configured bot"}.`,
      next ? "yellow" : "green");
  }, [identities, livePossible, pushToast]);

  const botLogin = identities[0]?.id ?? "bot";

  return (
    <Ctx.Provider value={{
      identities, botLogin, identity, setIdentity, dryRun, setDryRun: setDryRunGuarded, livePossible,
      liveError, retryLive, canMergeUpstream, login, review, toasts, pushToast, dismissToast, actionTick, reportResult,
    }}>
      {children}
    </Ctx.Provider>
  );
}
