import assert from "node:assert/strict";
import { test } from "node:test";
import {
  CHAT_BOTTOM_THRESHOLD_PX,
  isChatAtBottom,
  shouldFollowChatAfterScroll,
} from "./chatScroll.ts";

test("chat stays in follow mode while it is at the bottom", () => {
  assert.equal(isChatAtBottom({ scrollTop: 700, scrollHeight: 1000, clientHeight: 300 }), true);
  assert.equal(
    isChatAtBottom({
      scrollTop: 700 - CHAT_BOTTOM_THRESHOLD_PX,
      scrollHeight: 1000,
      clientHeight: 300,
    }),
    true,
  );
});

test("an upward scroll pauses follow mode immediately", () => {
  assert.equal(
    shouldFollowChatAfterScroll(700, { scrollTop: 699, scrollHeight: 1000, clientHeight: 300 }),
    false,
  );
});

test("scrolling back to the bottom resumes follow mode", () => {
  assert.equal(
    shouldFollowChatAfterScroll(500, { scrollTop: 700, scrollHeight: 1000, clientHeight: 300 }),
    true,
  );
});

test("a partial downward scroll remains paused", () => {
  assert.equal(
    shouldFollowChatAfterScroll(400, { scrollTop: 500, scrollHeight: 1000, clientHeight: 300 }),
    false,
  );
});
