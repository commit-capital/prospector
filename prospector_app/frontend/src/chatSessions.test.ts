import assert from "node:assert/strict";
import { test } from "node:test";
import {
  MAX_ARCHIVED_SESSIONS,
  MAX_STORED_SESSIONS,
  archiveSession,
  archivedSessions,
  capSessions,
  liveSessions,
  restoreSession,
  sessionForSubject,
  touchSession,
  type ChatSession,
} from "./chatSessions.ts";

const session = (id: string, lastActiveAt: number, archivedAt?: number): ChatSession => ({
  id, label: id, subject: { kind: "general", key: "general", label: id } as ChatSession["subject"],
  lastActiveAt, ...(archivedAt === undefined ? {} : { archivedAt }),
});

test("archiving keeps the session and moves it out of the live list", () => {
  const out = archiveSession([session("a", 2), session("b", 1)], "a", 10);
  assert.deepEqual(liveSessions(out).map((s) => s.id), ["b"]);
  assert.deepEqual(archivedSessions(out).map((s) => s.id), ["a"]);
  assert.equal(archivedSessions(out)[0].archivedAt, 10);
});

test("restoring an archived session puts it at the front of the live list", () => {
  const out = restoreSession([session("a", 5), session("b", 1, 3)], "b", 20);
  assert.deepEqual(liveSessions(out).map((s) => s.id), ["b", "a"]);
  assert.equal(archivedSessions(out).length, 0);
  assert.equal("archivedAt" in out.find((s) => s.id === "b")!, false);
});

test("archived sessions are listed most recently archived first", () => {
  const out = archivedSessions([session("a", 1, 5), session("b", 1, 9)]);
  assert.deepEqual(out.map((s) => s.id), ["b", "a"]);
});

test("the archive never pushes a live session out of storage", () => {
  const live = Array.from({ length: MAX_STORED_SESSIONS }, (_, i) => session(`l${i}`, i));
  const old = Array.from({ length: MAX_ARCHIVED_SESSIONS + 5 }, (_, i) => session(`x${i}`, 0, i));
  const out = capSessions([...live, ...old]);
  assert.equal(liveSessions(out).length, MAX_STORED_SESSIONS);
  assert.equal(archivedSessions(out).length, MAX_ARCHIVED_SESSIONS);
  assert.equal(archivedSessions(out)[0].id, `x${MAX_ARCHIVED_SESSIONS + 4}`);
});

test("a subject's session is its most recently active live one", () => {
  const about = (id: string, key: string, lastActiveAt: number, archivedAt?: number): ChatSession => ({
    ...session(id, lastActiveAt, archivedAt),
    subject: { kind: "pr", key, number: 1, label: key },
  });
  const sessions = [about("old", "pr:1", 1), about("new", "pr:1", 5), about("gone", "pr:1", 9, 10),
    about("other", "pr:2", 7)];
  assert.equal(sessionForSubject(sessions, "pr:1")?.id, "new");
  assert.equal(sessionForSubject(sessions, "pr:3"), null);
});

test("a session asked about another item is found under that item", () => {
  const pr2: ChatSession["subject"] = { kind: "pr", key: "pr:2", number: 2, label: "PR #2" };
  const out = touchSession([session("walk", 1)], "walk", 9, pr2);
  assert.equal(sessionForSubject(out, "pr:2")?.id, "walk");
  assert.equal(out[0].lastActiveAt, 9);
});
