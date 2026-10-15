import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode, type UIEvent } from "react";
import { useLocation } from "react-router";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { addSubjectParams, subjectFromLocation } from "../agentSubject";
import { api, type ChatReady, type FilterSpec } from "../api";
import { shouldFollowChatAfterScroll } from "../chatScroll";
import {
  archiveSession, archivedSessions, capSessions, liveSessions, restoreSession, touchSession,
  type ChatSession,
} from "../chatSessions";
import { chatName, parseTitles, titleKey, type ChatTitles } from "../chatTitles";
import { useRepoMeta } from "../RepoMetaContext";
import { useSpeechInput } from "../useSpeechInput";

function MicIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm5.3-3c0 3-2.54 5.1-5.3 5.1S6.7 14 6.7 11H5c0 3.41 2.72 6.23 6 6.72V21h2v-3.28c3.28-.48 6-3.3 6-6.72h-1.7z"/>
    </svg>
  );
}

function AgentReadinessHelp({ ready }: { ready: ChatReady }) {
  const codex = ready.provider === "codex";
  const cli = codex ? "Codex CLI" : "Claude Code CLI";
  const missing = codex ? "codex CLI not on PATH" : "claude CLI not on PATH";
  const installUrl = codex
    ? "https://developers.openai.com/codex"
    : "https://claude.com/claude-code";
  const installLabel = codex ? "install Codex" : "install Claude Code";
  const login = codex ? "codex login" : "claude auth login";

  return (
    <p className="muted small">
      The agent runs the <b>{cli}</b> on this computer, under your own login.{" "}
      {ready.problem === missing
        ? <>It isn't installed here — <a href={installUrl}
            target="_blank" rel="noreferrer">{installLabel}</a>, then check again.</>
        : ready.problem === "not logged in"
          ? <>It isn't logged in — run <code>{login}</code> in a terminal, then check again.</>
          : ready.problem === "Codex authentication is not file-backed"
            ? <>Its login is not file-backed. Set <code>cli_auth_credentials_store = "file"</code> in
                your Codex config, run <code>codex login</code>, then check again.</>
            : <>It didn't answer a status check ({ready.problem ?? "unknown"}).</>}
    </p>
  );
}

interface Anchor { file: string; line: number }
interface AgentCtx {
  anchor: Anchor | null;
  open: boolean;
  setOpen: (b: boolean) => void;
  askAbout: (file: string, line: number, question?: string) => void;
  clearAnchor: () => void;
  // The list view's current result (e.g. PR Explorer after its active
  // filters/search), so a general question can refer to "what I'm looking
  // at" without the operator re-listing numbers. A view sets this on
  // mount/data-change and clears it (null) on unmount; only one view owns it
  // at a time since only one such view is ever on screen.
  visible: VisibleSet | null;
  setVisible: (v: VisibleSet | null) => void;
}
/** The PR Explorer's current result: every matching number plus the filter
 *  spec that produced it, which the backend re-evaluates to ground a session. */
export type VisibleSet = { ids: number[]; spec: FilterSpec };
const Ctx = createContext<AgentCtx>({
  anchor: null, open: false, setOpen: () => {}, askAbout: () => {}, clearAnchor: () => {},
  visible: null, setVisible: () => {},
});
// eslint-disable-next-line react-refresh/only-export-components -- context hook co-located with its provider
export const useAgentPane = () => useContext(Ctx);

interface Msg { role: "user" | "assistant"; text: string }

// The session list and the active session belong to a deployment, so their
// keys carry the repository: two deployments served from one origin (two
// checkouts taking turns on a port) never hand each other their threads. The
// bare keys are the pre-scoping stores — read once when a scoped one is
// absent, removed on the first scoped save.
const SESSIONS_KEY = "agentpane-sessions-v2";
const ACTIVE_SESSION_KEY = "agentpane-active-session";
const TITLES_KEY = "agentpane-titles";
const scopedKey = (base: string, repo: string): string => `${base}@${repo}`;

function storedArray(key: string): unknown[] | null {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return null;
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function loadSessions(repo: string): ChatSession[] {
  const scoped = storedArray(scopedKey(SESSIONS_KEY, repo));
  if (scoped !== null) return scoped as ChatSession[];
  return (storedArray(SESSIONS_KEY) ?? []) as ChatSession[];
}

function loadActiveSessionId(repo: string): string | null {
  return localStorage.getItem(scopedKey(ACTIVE_SESSION_KEY, repo))
    || localStorage.getItem(ACTIVE_SESSION_KEY) || null;
}

function loadTitles(repo: string): ChatTitles {
  try {
    return parseTitles(localStorage.getItem(scopedKey(TITLES_KEY, repo)));
  } catch {
    return {};
  }
}

function newSessionId(): string {
  return `sess-${crypto.randomUUID()}`;
}

// Turn bare http(s) URLs in a user's message into clickable links while leaving
// the rest of the text verbatim (the bubble keeps white-space: pre-wrap, so we
// don't route it through markdown — that would collapse whitespace and reinterpret
// '#123'/'1.' the operator typed). Trailing sentence punctuation is left outside
// the href.
const URL_RE = /\bhttps?:\/\/[^\s<]+/g;
function linkifyUserText(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  URL_RE.lastIndex = 0;
  while ((m = URL_RE.exec(text)) !== null) {
    let url = m[0];
    const trail = url.match(/[.,;:!?)\]}'"]+$/);
    if (trail) url = url.slice(0, url.length - trail[0].length);
    const end = m.index + url.length;
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(<a key={m.index} href={url} target="_blank" rel="noopener noreferrer">{url}</a>);
    last = end;
    URL_RE.lastIndex = end;
  }
  if (last < text.length) out.push(text.slice(last));
  return out.length ? out : [text];
}


export function AgentPaneProvider({ children }: { children: ReactNode }) {
  const [anchor, setAnchor] = useState<Anchor | null>(null);
  const [open, setOpen] = useState(localStorage.getItem("agentpane-open") !== "0");
  const [pending, setPending] = useState<string | null>(null);
  const [visible, setVisible] = useState<VisibleSet | null>(null);
  const { meta } = useRepoMeta();
  // No pane on an unconfigured checkout (the wizard is the only page and the
  // chat API refuses there) or when the deployment turned agent support off.
  const enabled = !!meta && meta.configured && meta.agent_provider !== "none";

  const askAbout = useCallback((file: string, line: number, question?: string) => {
    setAnchor({ file, line });
    setOpen(true);
    if (question) setPending(question);
  }, []);
  const clearAnchor = useCallback(() => setAnchor(null), []);

  return (
    <Ctx.Provider value={{ anchor, open, setOpen, askAbout, clearAnchor, visible, setVisible }}>
      {children}
      {enabled && (
        <AgentPane anchor={anchor} open={open} setOpen={setOpen} clearAnchor={clearAnchor}
          pending={pending} clearPending={() => setPending(null)} visible={visible} />
      )}
    </Ctx.Provider>
  );
}

function AgentPane({ anchor, open, setOpen, clearAnchor, pending, clearPending, visible }: {
  anchor: Anchor | null; open: boolean; setOpen: (b: boolean) => void; clearAnchor: () => void;
  pending: string | null; clearPending: () => void; visible: VisibleSet | null;
}) {
  const loc = useLocation();
  const { meta } = useRepoMeta();
  const repo = meta?.repo ?? "";
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const speech = useSpeechInput((text) => setInput((p) => p ? p + " " + text : text));
  // Named sessions (#343): an operator-created thread that keeps its own subject
  // and history regardless of what page they're on. `sessions` is the stored
  // list (most-recently-active first); `activeSessionId` names the one currently
  // shown, or null for the default "whatever page I'm on" thread (unchanged
  // behavior for an operator who never opens Recent). `effectiveActiveId`
  // guards against a stale id left over from a session removed on another tab.
  const [sessions, setSessions] = useState<ChatSession[]>(() => loadSessions(repo));
  const [activeSessionId, setActiveSessionId] = useState<string | null>(
    () => loadActiveSessionId(repo));
  const activeSession = activeSessionId
    ? sessions.find((s) => s.id === activeSessionId && s.archivedAt === undefined) ?? null
    : null;
  const effectiveActiveId = activeSession ? activeSessionId : null;
  useEffect(() => {
    localStorage.setItem(scopedKey(SESSIONS_KEY, repo), JSON.stringify(sessions));
    localStorage.removeItem(SESSIONS_KEY);
  }, [sessions, repo]);
  const [titles, setTitles] = useState<ChatTitles>(() => loadTitles(repo));
  useEffect(() => {
    localStorage.setItem(scopedKey(TITLES_KEY, repo), JSON.stringify(titles));
  }, [titles, repo]);
  const [recentOpen, setRecentOpen] = useState(false);
  const [archivedOpen, setArchivedOpen] = useState(false);
  const recentRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!recentOpen) return;
    const onDown = (e: MouseEvent) => {
      if (!recentRef.current?.contains(e.target as Node)) setRecentOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setRecentOpen(false); };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [recentOpen]);
  useEffect(() => {
    const key = scopedKey(ACTIVE_SESSION_KEY, repo);
    if (effectiveActiveId) localStorage.setItem(key, effectiveActiveId);
    else localStorage.removeItem(key);
    localStorage.removeItem(ACTIVE_SESSION_KEY);
  }, [effectiveActiveId, repo]);
  // Subject stays frozen while streaming or while the thread has messages, so navigating
  // the UI doesn't reset an active conversation. The pane re-anchors to the current URL
  // only when the thread is empty — on first load or after "New chat". A named session
  // never drifts with the URL at all: its subject was frozen at creation.
  const liveSubj = subjectFromLocation(loc.pathname, loc.search);
  const [frozenSubj, setFrozenSubj] = useState(liveSubj);
  const hasConversation = msgs.length > 0;
  if (!activeSession && !streaming && !hasConversation
      && frozenSubj.key !== liveSubj.key)
    setFrozenSubj(liveSubj);
  const subj = activeSession
    ? { ...activeSession.subject, label: activeSession.label }
    : (streaming || hasConversation) ? frozenSubj : liveSubj;
  // Whatever PR list the operator is currently viewing (e.g. PR Explorer's
  // active filters) rides along regardless of subject — including when a PR's
  // flyout happens to be open on top of that list, which does NOT mean the
  // operator has abandoned the broader list they were browsing (#507). Purely
  // cosmetic; the thread/history key (subj.key) is unaffected, so
  // switching filters or peeking at a flyout never fragments the chat.
  const nVisible = visible?.ids.length ?? 0;
  const displaySubj = nVisible
    ? (subj.kind === "general"
        ? { ...subj, label: `the ${nVisible} filtered PR${nVisible === 1 ? "" : "s"}` }
        : { ...subj, label: `${subj.label} (+${nVisible} filtered PR${nVisible === 1 ? "" : "s"} in view)` })
    : subj;
  const securitySubject = subj.kind === "alert" || subj.kind === "advisory";
  const currentTitleKey = titleKey(effectiveActiveId, subj.key);
  const archived = archivedSessions(sessions);
  const currentName = chatName(titles, currentTitleKey, subj.label, hasConversation);
  const [paneW, setPaneW] = useState(() => Number(localStorage.getItem("agentpane-w")) || 360);
  // A turn that was cut off before the backend sent its `done` event (server
  // reload, dropped connection, killed subprocess). Distinct from a clean finish
  // so the operator sees it happened and can resume the claude session (-r)
  // rather than facing a silently-idle box.
  const [interrupted, setInterrupted] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  // Set true only when a `done` event arrives; `onerror` after this is a normal
  // post-completion socket close, not an interruption.
  const settledRef = useRef(false);
  const logRef = useRef<HTMLDivElement>(null);
  const followLatestRef = useRef(true);
  const lastScrollTopRef = useRef(0);
  const [followingLatest, setFollowingLatest] = useState(true);

  const setFollowLatest = useCallback((following: boolean): void => {
    followLatestRef.current = following;
    setFollowingLatest(following);
  }, []);

  const scrollToLatest = useCallback((): void => {
    const log = logRef.current;
    if (!log) return;
    log.scrollTop = log.scrollHeight;
    lastScrollTopRef.current = log.scrollTop;
    setFollowLatest(true);
  }, [setFollowLatest]);

  const handleLogScroll = useCallback((event: UIEvent<HTMLDivElement>): void => {
    const log = event.currentTarget;
    const following = shouldFollowChatAfterScroll(lastScrollTopRef.current, log);
    lastScrollTopRef.current = log.scrollTop;
    setFollowLatest(following);
  }, [setFollowLatest]);

  const toggle = () => { const n = !open; setOpen(n); localStorage.setItem("agentpane-open", n ? "1" : "0"); };
  const startResize = (e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX, startW = paneW;
    let latest = startW;
    // Freeze the width/padding transitions during the drag so the pane and the
    // header's compensating margin track the cursor in lockstep (#190). Lock
    // the cursor so it doesn't flicker off the handle.
    document.documentElement.classList.add("ap-resizing");
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    const move = (ev: MouseEvent) => { latest = Math.min(window.innerWidth * 0.6, Math.max(280, startW + (ev.clientX - startX))); setPaneW(latest); };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      document.documentElement.classList.remove("ap-resizing");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      localStorage.setItem("agentpane-w", String(Math.round(latest)));
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  const ctxParams = () => {
    const p = new URLSearchParams();
    addSubjectParams(p, subj);
    if (effectiveActiveId) p.set("chat_id", effectiveActiveId);
    return p;
  };

  // load this thread's history when the subject or the active session changes
  useEffect(() => {
    const p = ctxParams();
    api.chatHistory(p).catch(() => ({ messages: [] })).then((d) => {
      setFollowLatest(true);
      setMsgs(d.messages);
    });
    clearAnchor();
    return () => esRef.current?.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subj.key, effectiveActiveId]);

  useLayoutEffect(() => {
    if (!open || !followLatestRef.current) return;
    scrollToLatest();
  }, [msgs, open, scrollToLatest]);

  // reserve left space for the sidebar (tracks the resizable width); collapses to
  // a thin rail. useLayoutEffect so the page never paints un-shifted first. The
  // cleanup gives the space back when the pane unmounts (agent support off).
  useLayoutEffect(() => {
    document.documentElement.style.setProperty("--ap-w", open ? `${paneW}px` : "38px");
    return () => { document.documentElement.style.removeProperty("--ap-w"); };
  }, [open, paneW]);

  // Whether this machine can run the agent at all — the selected local CLI's
  // presence and login. Checked once per pane-open; not-ready swaps the
  // composer for a fix-it notice with a re-check.
  const [ready, setReady] = useState<ChatReady | null>(null);
  const [checkingReady, setCheckingReady] = useState(false);
  const checkReady = useCallback(() => {
    setCheckingReady(true);
    api.chatReady().then(setReady).catch(() => setReady(null))
      .finally(() => setCheckingReady(false));
  }, []);
  useEffect(() => {
    if (!open) return;
    let stale = false;
    api.chatReady().then((r) => { if (!stale) setReady(r); }).catch(() => {});
    return () => { stale = true; };
  }, [open]);

  const send = useCallback((text: string) => {
    if (!text.trim() || streaming) return;
    speech.stop();
    setFollowLatest(true);
    setMsgs((m) => [...m, { role: "user", text: (anchor ? `[${anchor.file}:${anchor.line}] ` : "") + text }, { role: "assistant", text: "" }]);
    setInput("");
    setStreaming(true);
    setInterrupted(false);
    settledRef.current = false;
    // The thread's first message names it; later messages keep that name.
    if (!hasConversation && !titles[currentTitleKey]) {
      const key = currentTitleKey;
      api.chatTitle(text)
        .then(({ title }) => { if (title) setTitles((t) => ({ ...t, [key]: title })); })
        .catch(() => {});
    }
    const p = ctxParams();
    p.set("q", text);
    if (anchor) { p.set("file", anchor.file); p.set("line", String(anchor.line)); }
    // PR Explorer's filter grounds agent sessions; the backend evaluates it.
    if (visible && visible.ids.length) p.set("spec", JSON.stringify(visible.spec));
    const es = new EventSource(`/api/chat?${p}`);
    esRef.current = es;
    es.addEventListener("delta", (e: MessageEvent) => {
      setMsgs((m) => { const c = [...m]; c[c.length - 1] = { role: "assistant", text: c[c.length - 1].text + e.data }; return c; });
    });
    es.addEventListener("done", () => { settledRef.current = true; es.close(); setStreaming(false); });
    es.onerror = () => { es.close(); setStreaming(false); if (!settledRef.current) setInterrupted(true); };
    // Fresh activity floats a named session back to the top of Recent.
    if (effectiveActiveId) {
      const id = effectiveActiveId;
      setSessions((prev) => touchSession(prev, id, Date.now()));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [streaming, anchor, subj.key, effectiveActiveId, speech.stop, visible, setFollowLatest,
    hasConversation, titles, currentTitleKey]);

  // Resume a cut-off turn: nudge the (still-resumable) claude session to continue
  // where it left off. The backend re-attaches via -r, so the agent keeps its
  // full context — this is the manual "re-kick" made a one-click action.
  const resume = useCallback(() => {
    if (streaming) return;
    setInterrupted(false);
    send("Continue where you left off — the previous turn was cut off before it finished.");
  }, [streaming, send]);

  // stop an in-flight answer (#14): kill the backend subprocess and release the UI.
  // Deltas already streamed stay on screen; the backend persists the partial reply.
  const stop = useCallback(() => {
    if (!streaming) return;
    esRef.current?.close();
    setStreaming(false);
    const p = ctxParams();
    fetch(`/api/chat/stop?${p}`, { method: "POST" }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [streaming, subj.key, effectiveActiveId]);

  // Start a brand-new named session (#343): a fresh backend thread (new chat_id)
  // rather than just clearing the visible log, so the agent stops resuming a
  // weeks-old claude session full of stale context — the bug behind "it keeps
  // referring to things in our previous conversation." Named from wherever the
  // operator is looking right now; that subject is then frozen for this session.
  const createSession = useCallback(() => {
    setRecentOpen(false);
    esRef.current?.close();
    setStreaming(false);
    setInterrupted(false);
    clearAnchor();
    speech.stop();
    const s = subjectFromLocation(loc.pathname, loc.search);
    const session: ChatSession = {
      id: newSessionId(), label: s.label, subject: s, lastActiveAt: Date.now(),
    };
    setSessions((prev) => capSessions([session, ...prev]));
    setActiveSessionId(session.id);
    setMsgs([]);
    setInput("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clearAnchor, loc.pathname, loc.search, speech.stop]);

  // Switch to another stored session, or back to the live "whatever page I'm on"
  // thread (id null) — the entry always listed first under Recent. An archived
  // session comes back to the live list.
  const switchSession = useCallback((id: string | null) => {
    setRecentOpen(false);
    if (id === effectiveActiveId) return;
    esRef.current?.close();
    setStreaming(false);
    setInterrupted(false);
    clearAnchor();
    speech.stop();
    setInput("");
    setMsgs([]);
    if (id) {
      setSessions((prev) => restoreSession(prev, id, Date.now()));
    } else {
      setFrozenSubj(subjectFromLocation(loc.pathname, loc.search));
    }
    setActiveSessionId(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveActiveId, clearAnchor, speech.stop, loc.pathname, loc.search]);

  // Archive a named session: it moves from Recent to Recent's Archived flyout,
  // keeping its title. If it is the session the operator is currently viewing,
  // return to the live current-page thread; an in-flight answer also needs
  // stopping so it does not keep running after its UI has disappeared. The
  // transcript remains in the backend store.
  const archive = useCallback((id: string) => {
    const removingActive = id === effectiveActiveId;
    if (removingActive) {
      esRef.current?.close();
      if (streaming) {
        const p = new URLSearchParams({ chat_id: id });
        fetch(`/api/chat/stop?${p}`, { method: "POST" }).catch(() => {});
      }
      setStreaming(false);
      setInterrupted(false);
      clearAnchor();
      speech.stop();
      setInput("");
      setMsgs([]);
      setFrozenSubj(subjectFromLocation(loc.pathname, loc.search));
      setActiveSessionId(null);
    }
    setSessions((prev) => archiveSession(prev, id, Date.now()));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveActiveId, streaming, clearAnchor, speech.stop, loc.pathname, loc.search]);

  // Esc stops an in-flight answer (the textarea is disabled while streaming, so
  // listen on the window).
  useEffect(() => {
    if (!streaming) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") stop(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [streaming, stop]);

  // auto-send when the diff popup's "Explain" provided a question
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- act on the question handed off from the diff popup
    if (pending) { setOpen(true); send(pending); clearPending(); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending]);

  return (
    <div className={`agentpane ${open ? "open" : "collapsed"}`}>
      {open ? (
        <>
          <div className="agentpane-bar" onClick={toggle} title="Collapse">
            <span className="ap-title">🤖 Ask the agent</span>
            <span className="ap-toggle">‹</span>
            <span className="ap-subject">about <b>{displaySubj.label}</b>
              {hasConversation && (
                <button className="ap-new-chat" onClick={(e) => { e.stopPropagation(); createSession(); }}
                  title="Start a new chat session">↺</button>
              )}
            </span>
            {anchor && <span className="ap-anchor" onClick={(e) => { e.stopPropagation(); clearAnchor(); }}>📍 {anchor.file.split("/").pop()}:{anchor.line} ✕</span>}
          </div>
          <div className="agentpane-body">
            <div className="ap-chat-switcher" onClick={(e) => e.stopPropagation()}>
              <span className="ap-chat-current" title={currentName}>{currentName}</span>
              <button className="ap-session-new" onClick={createSession} title="Start a new chat session">+ New</button>
              <div className="ap-recent" ref={recentRef}>
                <button
                  className="ap-recent-toggle"
                  onClick={() => { setRecentOpen((o) => !o); setArchivedOpen(false); }}
                  aria-haspopup="menu"
                  aria-expanded={recentOpen}
                  title="Recent conversations"
                >Recent ▾</button>
                {recentOpen && (
                  <div className="ap-recent-menu" role="menu">
                    <div className={`ap-recent-item${!effectiveActiveId ? " active" : ""}`}>
                      <button className="ap-recent-select" role="menuitem" onClick={() => switchSession(null)}>
                        <span className="ap-recent-name">
                          {titles[titleKey(null, liveSubj.key)] ?? "Current page chat"}
                        </span>
                        <span className="ap-recent-subject">current page · {liveSubj.label}</span>
                      </button>
                    </div>
                    {liveSessions(sessions).map((s) => (
                      <div key={s.id} className={`ap-recent-item${s.id === effectiveActiveId ? " active" : ""}`}>
                        <button className="ap-recent-select" role="menuitem" onClick={() => switchSession(s.id)}>
                          <span className="ap-recent-name">{titles[s.id] ?? s.label}</span>
                          <span className="ap-recent-subject">{s.subject.label}</span>
                        </button>
                        <button
                          className="ap-session-remove"
                          onClick={() => archive(s.id)}
                          title={`Archive ${titles[s.id] ?? s.label}`}
                          aria-label={`Archive ${titles[s.id] ?? s.label}`}
                        >×</button>
                      </div>
                    ))}
                    {archived.length > 0 && (
                      <div className="ap-recent-archived">
                        <button className="ap-recent-select ap-recent-archived-toggle" aria-expanded={archivedOpen}
                          onClick={() => setArchivedOpen((o) => !o)}>
                          <span className="ap-recent-name">Archived ({archived.length}) {archivedOpen ? "▾" : "›"}</span>
                        </button>
                        {archivedOpen && archived.map((s) => (
                          <div key={s.id} className="ap-recent-item">
                            <button className="ap-recent-select" role="menuitem" onClick={() => switchSession(s.id)}
                              title="Restore to Recent">
                              <span className="ap-recent-name">{titles[s.id] ?? s.label}</span>
                              <span className="ap-recent-subject">{s.subject.label}</span>
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
            <div className="agentpane-log-wrap">
              <div className="agentpane-log" ref={logRef} onScroll={handleLogScroll}>
                {msgs.length === 0 && (
                  <p className="muted small ap-hint">
                    Ask anything about {displaySubj.label} — {securitySubject
                      ? <>"is this still exploitable?", "do you agree with the find-fixed result?".</>
                      : <>"is this safe to merge?", "what's the root cause here?". Click a diff line for a per-line popup.</>}
                  </p>
                )}
                {msgs.map((m, i) => (
                  <div key={i} className={`bubble ${m.role}`}>
                    {m.role === "assistant"
                      ? (m.text
                          ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.text}</ReactMarkdown>
                          : (streaming && i === msgs.length - 1 ? <span className="cursor">▋</span> : null))
                      : linkifyUserText(m.text)}
                  </div>
                ))}
              </div>
              {!followingLatest && msgs.length > 0 && (
                <button className="ap-follow-latest" type="button" onClick={scrollToLatest}>
                  <span aria-hidden="true">↓</span> {streaming ? "Follow live" : "Jump to latest"}
                </button>
              )}
            </div>
            {interrupted && !streaming && (
              <div className="ap-interrupted">
                <span>⚠ The agent's turn was cut off before it finished (the stream dropped).</span>
                <button className="ap-resume" onClick={resume}>Resume</button>
              </div>
            )}
            {ready && !ready.ok ? (
              <div className="ap-not-ready">
                <AgentReadinessHelp ready={ready} />
                <button className="btn-secondary sm" disabled={checkingReady} onClick={checkReady}>
                  {checkingReady ? "checking…" : "Check again"}
                </button>
              </div>
            ) : (
            <div className="agentpane-input">
              <div className="ap-input-wrap">
                <textarea rows={2} placeholder={streaming ? "Agent is thinking…" : `Ask about ${displaySubj.label}…`}
                  value={input} disabled={streaming} onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); } }} />
                {speech.supported && (
                  <button
                    className={`ap-mic-btn${speech.active ? " active" : ""}`}
                    onClick={speech.toggle}
                    title={speech.active ? "Stop voice input" : "Voice input (speech to text)"}
                    aria-label={speech.active ? "Stop voice input" : "Start voice input"}
                    disabled={streaming}
                  >
                    <MicIcon />
                  </button>
                )}
              </div>
              {streaming
                ? <button className="btn-stop sm" onClick={stop} title="Stop the agent (Esc)">■ Stop</button>
                : <button className="btn-primary sm" disabled={!input.trim()} onClick={() => send(input)}>Send</button>}
            </div>
            )}
            {speech.error && <div className="ap-mic-error">Mic error: {speech.error}. Check browser permissions.</div>}
          </div>
          <div className="agentpane-resize" onMouseDown={startResize} title="Drag to resize width" />
        </>
      ) : (
        <button className="agentpane-tab" onClick={toggle} title="Ask the agent">
          <span className="ap-tab-icon">🤖</span>
          <span className="ap-tab-caret">›</span>
        </button>
      )}
    </div>
  );
}
