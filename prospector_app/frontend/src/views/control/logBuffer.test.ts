import assert from "node:assert/strict";
import { test } from "node:test";
import { SKIPPED, appendLines, nextLines } from "./logBuffer.ts";

test("new lines append and the buffer keeps only the newest", () => {
  assert.deepEqual(appendLines(["a", "b"], ["c"], 5), ["a", "b", "c"]);
  assert.deepEqual(appendLines(["a", "b", "c"], ["d", "e"], 3), ["c", "d", "e"]);
  assert.deepEqual(appendLines([], [], 3), []);
});

test("a skipped read restarts the view at the current tail", () => {
  assert.deepEqual(nextLines(["a"], { lines: ["b"], skipped: false }, 5), ["a", "b"]);
  assert.deepEqual(nextLines(["a"], { lines: ["z"], skipped: true }, 5), [SKIPPED, "z"]);
});
