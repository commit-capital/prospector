import assert from "node:assert/strict";
import { test } from "node:test";
import { appendLines } from "./logBuffer.ts";

test("new lines append and the buffer keeps only the newest", () => {
  assert.deepEqual(appendLines(["a", "b"], ["c"], 5), ["a", "b", "c"]);
  assert.deepEqual(appendLines(["a", "b", "c"], ["d", "e"], 3), ["c", "d", "e"]);
  assert.deepEqual(appendLines([], [], 3), []);
});
