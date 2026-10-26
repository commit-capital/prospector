import type { AgentSubject } from "./agentSubject";

// A named conversation thread (#343), independent of whatever the operator is
// currently viewing — created via "+ New" or by a page thread's first message,
// switched between via the Recent menu. Its subject is the item it was last
// asked about, which it keeps while the operator is on a general page. Persisted
// to localStorage only (no cross-device sync); the backend thread itself is
// addressed by `id` as an opaque chat_id and needs no record of the label.
// An archived session (`archivedAt` set) moves to the Recent menu's Archived
// flyout.
export interface ChatSession {
  id: string;
  label: string;
  subject: AgentSubject;
  lastActiveAt: number;
  archivedAt?: number;
}

export const MAX_STORED_SESSIONS = 20;
export const MAX_ARCHIVED_SESSIONS = 50;

const byRecency = (a: ChatSession, b: ChatSession): number => b.lastActiveAt - a.lastActiveAt;

export function liveSessions(sessions: ChatSession[]): ChatSession[] {
  return sessions.filter((s) => s.archivedAt === undefined).sort(byRecency);
}

export function archivedSessions(sessions: ChatSession[]): ChatSession[] {
  return sessions.filter((s) => s.archivedAt !== undefined)
    .sort((a, b) => (b.archivedAt ?? 0) - (a.archivedAt ?? 0));
}

// The stored list: live sessions most recent first, then the archive, each
// held to its own cap so archiving never pushes a live session out.
export function capSessions(sessions: ChatSession[]): ChatSession[] {
  return [
    ...liveSessions(sessions).slice(0, MAX_STORED_SESSIONS),
    ...archivedSessions(sessions).slice(0, MAX_ARCHIVED_SESSIONS),
  ];
}

export function archiveSession(sessions: ChatSession[], id: string, now: number): ChatSession[] {
  return capSessions(sessions.map((s) => s.id === id ? { ...s, archivedAt: now } : s));
}

// Brings an archived session back to the front of the live list.
export function restoreSession(sessions: ChatSession[], id: string, now: number): ChatSession[] {
  return capSessions(sessions.map((s) => {
    if (s.id !== id) return s;
    const restored: ChatSession = { ...s, lastActiveAt: now };
    delete restored.archivedAt;
    return restored;
  }));
}

export function touchSession(sessions: ChatSession[], id: string, now: number,
  subject: AgentSubject): ChatSession[] {
  return capSessions(sessions.map((s) => s.id === id
    ? { ...s, label: subject.label, subject, lastActiveAt: now } : s));
}

// The most recently active live session about this subject.
export function sessionForSubject(sessions: ChatSession[], subjectKey: string): ChatSession | null {
  return liveSessions(sessions).find((s) => s.subject.key === subjectKey) ?? null;
}
