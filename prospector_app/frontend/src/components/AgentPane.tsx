import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useLocation } from "react-router";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useSpeechInput } from "../useSpeechInput";

function MicIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm5.3-3c0 3-2.54 5.1-5.3 5.1S6.7 14 6.7 11H5c0 3.41 2.72 6.23 6 6.72V21h2v-3.28c3.28-.48 6-3.3 6-6.72h-1.7z"/>
    </svg>
  );
}

interface Anchor { file: string; line: number }
interface AgentCtx {
  anchor: Anchor | null;
  open: boolean;
  setOpen: (b: boolean) => void;
  askAbout: (file: string, line: number, question?: string) => void;
  clearAnchor: () => void;
  // The PR numbers currently visible in a list view (e.g. PR Explorer after its
  // active filters/search), so a general question can refer to "what I'm
  // looking at" without the operator re-listing numbers. A view sets this on
  // mount/data-change and clears it (null) on unmount; only one view owns it
  // at a time since only one such view is ever on screen.
  visiblePrs: number[] | null;
  setVisiblePrs: (ids: number[] | null) => void;
}
const Ctx = createContext<AgentCtx>({
  anchor: null, open: false, setOpen: () => {}, askAbout: () => {}, clearAnchor: () => {},
  visiblePrs: null, setVisiblePrs: () => {},
});
// eslint-disable-next-line react-refresh/only-export-components -- context hook co-located with its provider
export const useAgentPane = () => useContext(Ctx);

interface Msg { role: "user" | "assistant"; text: string }

// A named conversation thread (#343), independent of whatever the operator is
// currently viewing — created via "+ New", switched between via the sessions
// strip. subjKind/subjId are frozen at creation so the session keeps asking
// about its own subject even after the operator navigates elsewhere. Persisted
// to localStorage only (no cross-device sync); the backend thread itself is
// addressed by `id` as an opaque chat_id and needs no record of the label.
interface ChatSession {
  id: string;
  label: string;
  subjKind: "pr" | "cluster" | "issue" | "general";
  subjId: number | null;
  lastActiveAt: number;
}

const SESSIONS_KEY = "agentpane-sessions-v1";
const ACTIVE_SESSION_KEY = "agentpane-active-session";
const MAX_STORED_SESSIONS = 20;
const MAX_VISIBLE_SESSION_PILLS = 6;

function loadSessions(): ChatSession[] {
  try {
    const raw = localStorage.getItem(SESSIONS_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? (parsed as ChatSession[]) : [];
  } catch {
    return [];
  }
}

function newSessionId(): string {
  const rand = typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : Math.random().toString(36).slice(2);
  return `sess-${rand}`;
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

// Cap how many visible-PR numbers ride on the EventSource URL (and how many the
// backend renders into the prompt) — matches chat.py's _VISIBLE_PRS_CAP.
const VISIBLE_PRS_CAP = 150;

export function AgentPaneProvider({ children }: { children: ReactNode }) {
  const [anchor, setAnchor] = useState<Anchor | null>(null);
  const [open, setOpen] = useState(localStorage.getItem("agentpane-open") !== "0");
  const [pending, setPending] = useState<string | null>(null);
  const [visiblePrs, setVisiblePrs] = useState<number[] | null>(null);

  const askAbout = useCallback((file: string, line: number, question?: string) => {
    setAnchor({ file, line });
    setOpen(true);
    if (question) setPending(question);
  }, []);
  const clearAnchor = useCallback(() => setAnchor(null), []);

  return (
    <Ctx.Provider value={{ anchor, open, setOpen, askAbout, clearAnchor, visiblePrs, setVisiblePrs }}>
      {children}
      <AgentPane anchor={anchor} open={open} setOpen={setOpen} clearAnchor={clearAnchor}
        pending={pending} clearPending={() => setPending(null)} visiblePrs={visiblePrs} />
    </Ctx.Provider>
  );
}

function subjectFromLocation(pathname: string, search: string) {
  const sp = new URLSearchParams(search);
  const pr = sp.get("pr");
  if (pr) return { kind: "pr" as const, id: Number(pr), label: `PR #${pr}` };
  const issue = sp.get("issue");
  if (issue) return { kind: "issue" as const, id: Number(issue), label: `issue #${issue}` };
  const m = pathname.match(/^\/clusters\/(\d+)/);
  if (m) return { kind: "cluster" as const, id: Number(m[1]), label: `cluster ${m[1]}` };
  return { kind: "general" as const, id: null, label: "this app or codebase" };
}

function AgentPane({ anchor, open, setOpen, clearAnchor, pending, clearPending, visiblePrs }: {
  anchor: Anchor | null; open: boolean; setOpen: (b: boolean) => void; clearAnchor: () => void;
  pending: string | null; clearPending: () => void; visiblePrs: number[] | null;
}) {
  const loc = useLocation();
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const speech = useSpeechInput((text) => setInput((p) => p ? p + " " + text : text));
  // Named sessions (#343): an operator-created thread that keeps its own subject
  // and history regardless of what page they're on. `sessions` is the stored
  // list (most-recently-active first); `activeSessionId` names the one currently
  // shown, or null for the default "whatever page I'm on" thread (unchanged
  // behavior for an operator who never opens the strip). `effectiveActiveId`
  // guards against a stale id left over from a session removed on another tab.
  const [sessions, setSessions] = useState<ChatSession[]>(() => loadSessions());
  const [activeSessionId, setActiveSessionId] = useState<string | null>(
    () => localStorage.getItem(ACTIVE_SESSION_KEY) || null);
  const activeSession = activeSessionId ? sessions.find((s) => s.id === activeSessionId) ?? null : null;
  const effectiveActiveId = activeSession ? activeSessionId : null;
  useEffect(() => { localStorage.setItem(SESSIONS_KEY, JSON.stringify(sessions)); }, [sessions]);
  useEffect(() => {
    if (effectiveActiveId) localStorage.setItem(ACTIVE_SESSION_KEY, effectiveActiveId);
    else localStorage.removeItem(ACTIVE_SESSION_KEY);
  }, [effectiveActiveId]);
  // Subject stays frozen while streaming or while the thread has messages, so navigating
  // the UI doesn't reset an active conversation. The pane re-anchors to the current URL
  // only when the thread is empty — on first load or after "New chat". A named session
  // never drifts with the URL at all: its subject was frozen at creation.
  const liveSubj = subjectFromLocation(loc.pathname, loc.search);
  // Keep the strip relevant to the page the operator is on. A session from a
  // different subject stays persisted and will reappear when that subject is
  // revisited; only the active session remains visible across navigation so it
  // is never possible for the pane to show a conversation with no selected pill.
  const visibleSessions = sessions
    .filter((s) => s.id === effectiveActiveId
      || (s.subjKind === liveSubj.kind && s.subjId === liveSubj.id))
    .slice(0, MAX_VISIBLE_SESSION_PILLS);
  const [frozenSubj, setFrozenSubj] = useState(liveSubj);
  const hasConversation = msgs.length > 0;
  if (!activeSession && !streaming && !hasConversation
      && (frozenSubj.kind !== liveSubj.kind || frozenSubj.id !== liveSubj.id))
    setFrozenSubj(liveSubj);
  const subj = activeSession
    ? { kind: activeSession.subjKind, id: activeSession.subjId, label: activeSession.label }
    : (streaming || hasConversation) ? frozenSubj : liveSubj;
  // Whatever PR list the operator is currently viewing (e.g. PR Explorer's
  // active filters) rides along regardless of subject — including when a PR's
  // flyout happens to be open on top of that list, which does NOT mean the
  // operator has abandoned the broader list they were browsing (#507). Purely
  // cosmetic; the thread/history key (subj.kind, subj.id) is unaffected, so
  // switching filters or peeking at a flyout never fragments the chat.
  const displaySubj = visiblePrs && visiblePrs.length
    ? (subj.kind === "general"
        ? { ...subj, label: `the ${visiblePrs.length} filtered PR${visiblePrs.length === 1 ? "" : "s"}` }
        : { ...subj, label: `${subj.label} (+${visiblePrs.length} filtered PR${visiblePrs.length === 1 ? "" : "s"} in view)` })
    : subj;
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

  const toggle = () => { const n = !open; setOpen(n); localStorage.setItem("agentpane-open", n ? "1" : "0"); };
  const startResize = (e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX, startW = paneW;
    let latest = startW;
    // Freeze the width/padding transitions during the drag so the pane and the
    // topbar's compensating margin track the cursor in lockstep — otherwise the
    // transitioned .app padding lags the instant topbar margin and the header
    // jitters (#190). Lock the cursor so it doesn't flicker off the handle.
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
    if (subj.kind === "pr") p.set("pr", String(subj.id));
    else if (subj.kind === "cluster") p.set("cluster", String(subj.id));
    else if (subj.kind === "issue") p.set("issue", String(subj.id));
    if (effectiveActiveId) p.set("chat_id", effectiveActiveId);
    return p;
  };

  // load this thread's history when the subject or the active session changes
  useEffect(() => {
    const p = ctxParams();
    fetch(`/api/chat/history?${p}`).then((r) => r.json()).then((d) => setMsgs(d.messages || []));
    clearAnchor();
    return () => esRef.current?.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subj.kind, subj.id, effectiveActiveId]);

  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; }, [msgs]);

  // reserve left space for the sidebar (tracks the resizable width); collapses to
  // a thin rail. useLayoutEffect so the page never paints un-shifted first.
  useLayoutEffect(() => {
    document.documentElement.style.setProperty("--ap-w", open ? `${paneW}px` : "38px");
  }, [open, paneW]);

  const send = useCallback((text: string) => {
    if (!text.trim() || streaming) return;
    speech.stop();
    setMsgs((m) => [...m, { role: "user", text: (anchor ? `[${anchor.file}:${anchor.line}] ` : "") + text }, { role: "assistant", text: "" }]);
    setInput("");
    setStreaming(true);
    setInterrupted(false);
    settledRef.current = false;
    const p = ctxParams();
    p.set("q", text);
    if (anchor) { p.set("file", anchor.file); p.set("line", String(anchor.line)); }
    // Every question carries the operator's currently-visible PR list (e.g. PR
    // Explorer's active filters), not just general ones, so "review these"
    // still works even when a PR's flyout happens to be open on top of that
    // list (#507) — the flyout doesn't mean the operator stopped browsing the
    // broader set. It rides alongside the pr/cluster subject's own full
    // context (diff, findings, member signals) as supplementary information.
    if (visiblePrs && visiblePrs.length) {
      const capped = visiblePrs.slice(0, VISIBLE_PRS_CAP);
      p.set("prs", capped.join(","));
      if (visiblePrs.length > capped.length) p.set("prs_total", String(visiblePrs.length));
    }
    const es = new EventSource(`/api/chat?${p}`);
    esRef.current = es;
    es.addEventListener("delta", (e: MessageEvent) => {
      setMsgs((m) => { const c = [...m]; c[c.length - 1] = { role: "assistant", text: c[c.length - 1].text + e.data }; return c; });
    });
    es.addEventListener("replace", (e: MessageEvent) => {
      setMsgs((m) => { const c = [...m]; c[c.length - 1] = { role: "assistant", text: e.data }; return c; });
    });
    es.addEventListener("done", () => { settledRef.current = true; es.close(); setStreaming(false); });
    es.onerror = () => { es.close(); setStreaming(false); if (!settledRef.current) setInterrupted(true); };
    // Fresh activity floats a named session back to the front of the strip.
    if (effectiveActiveId) {
      const id = effectiveActiveId;
      setSessions((prev) => [...prev]
        .map((s) => s.id === id ? { ...s, lastActiveAt: Date.now() } : s)
        .sort((a, b) => b.lastActiveAt - a.lastActiveAt));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [streaming, anchor, subj.kind, subj.id, effectiveActiveId, speech.stop, visiblePrs]);

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
  }, [streaming, subj.kind, subj.id, effectiveActiveId]);

  // Start a brand-new named session (#343): a fresh backend thread (new chat_id)
  // rather than just clearing the visible log, so the agent stops resuming a
  // weeks-old claude session full of stale context — the bug behind "it keeps
  // referring to things in our previous conversation." Named from wherever the
  // operator is looking right now; that subject is then frozen for this session.
  const createSession = useCallback(() => {
    esRef.current?.close();
    setStreaming(false);
    setInterrupted(false);
    clearAnchor();
    speech.stop();
    const s = subjectFromLocation(loc.pathname, loc.search);
    const session: ChatSession = {
      id: newSessionId(), label: s.label, subjKind: s.kind, subjId: s.id, lastActiveAt: Date.now(),
    };
    setSessions((prev) => [session, ...prev].slice(0, MAX_STORED_SESSIONS));
    setActiveSessionId(session.id);
    setMsgs([]);
    setInput("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clearAnchor, loc.pathname, loc.search, speech.stop]);

  // Switch to another stored session, or back to the live "whatever page I'm on"
  // thread (id null) — the pseudo-pill always shown first in the strip.
  const switchSession = useCallback((id: string | null) => {
    if (id === effectiveActiveId) return;
    esRef.current?.close();
    setStreaming(false);
    setInterrupted(false);
    clearAnchor();
    speech.stop();
    setInput("");
    setMsgs([]);
    if (id) {
      setSessions((prev) => [...prev]
        .map((s) => s.id === id ? { ...s, lastActiveAt: Date.now() } : s)
        .sort((a, b) => b.lastActiveAt - a.lastActiveAt));
    } else {
      setFrozenSubj(subjectFromLocation(loc.pathname, loc.search));
    }
    setActiveSessionId(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveActiveId, clearAnchor, speech.stop, loc.pathname, loc.search]);

  // Remove a named session from the persistent strip. If it is the session the
  // operator is currently viewing, return to the live current-page thread; an
  // in-flight answer also needs stopping so it does not keep running after its
  // UI has disappeared. The transcript remains in the backend store (the
  // browser only owns this list of session shortcuts).
  const removeSession = useCallback((id: string) => {
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
    setSessions((prev) => prev.filter((s) => s.id !== id));
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
            <div className="ap-sessions-strip" onClick={(e) => e.stopPropagation()}>
              <button
                className={`ap-session-pill live${!effectiveActiveId ? " active" : ""}`}
                onClick={() => switchSession(null)}
                title={`Current page: ${liveSubj.label}`}
              >{liveSubj.label}</button>
              {visibleSessions.map((s) => (
                <span
                  key={s.id}
                  className={`ap-session-pill${s.id === effectiveActiveId ? " active" : ""}`}
                >
                  <button
                    className="ap-session-select"
                    onClick={() => switchSession(s.id)}
                    title={s.label}
                  >{s.label}</button>
                  <button
                    className="ap-session-remove"
                    onClick={() => removeSession(s.id)}
                    title={`Remove ${s.label} session`}
                    aria-label={`Remove ${s.label} session`}
                  >×</button>
                </span>
              ))}
              <button className="ap-session-new" onClick={createSession} title="Start a new chat session">+ New</button>
            </div>
            <div className="agentpane-log" ref={logRef}>
              {msgs.length === 0 && <p className="muted small ap-hint">Ask anything about {displaySubj.label} — "is this safe to merge?", "what's the root cause here?". Click a diff line for a per-line popup.</p>}
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
            {interrupted && !streaming && (
              <div className="ap-interrupted">
                <span>⚠ The agent's turn was cut off before it finished (the stream dropped).</span>
                <button className="ap-resume" onClick={resume}>Resume</button>
              </div>
            )}
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
