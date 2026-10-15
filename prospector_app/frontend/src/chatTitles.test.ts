import assert from "node:assert/strict";
import { test } from "node:test";
import { chatName, parseTitles, titleKey } from "./chatTitles.ts";

test("a named session is keyed by its id, the current-page thread by its subject", () => {
  assert.equal(titleKey("sess-1", "pr:5"), "sess-1");
  assert.equal(titleKey(null, "pr:5"), "page:pr:5");
});

test("a thread shows its title, else its subject once it has messages, else New chat", () => {
  const titles = { "sess-1": "Design review failures" };
  assert.equal(chatName(titles, "sess-1", "this app or codebase", true), "Design review failures");
  assert.equal(chatName(titles, "sess-2", "this app or codebase", true), "this app or codebase");
  assert.equal(chatName(titles, "sess-2", "this app or codebase", false), "New chat");
});

test("stored titles keep only non-empty strings and survive a corrupt store", () => {
  assert.deepEqual(parseTitles(JSON.stringify({ a: "One", b: 2, c: "" })), { a: "One" });
  assert.deepEqual(parseTitles("not json"), {});
  assert.deepEqual(parseTitles("[1]"), {});
  assert.deepEqual(parseTitles(null), {});
});
