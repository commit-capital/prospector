import assert from "node:assert/strict";
import { test } from "node:test";
import { ignoreTableKey, nextRowIndex } from "./tableKeys.ts";

const plain = { metaKey: false, ctrlKey: false, altKey: false };

test("either key enters an uncursored table at the top", () => {
  assert.equal(nextRowIndex(null, "j", 5), 0);
  assert.equal(nextRowIndex(null, "k", 5), 0);
});

test("j moves down and k moves up", () => {
  assert.equal(nextRowIndex(1, "j", 5), 2);
  assert.equal(nextRowIndex(1, "k", 5), 0);
});

test("the cursor clamps at both ends", () => {
  assert.equal(nextRowIndex(4, "j", 5), 4);
  assert.equal(nextRowIndex(0, "k", 5), 0);
});

test("a cursor past the end of a shrunken table clamps to the last row", () => {
  assert.equal(nextRowIndex(9, "j", 3), 2);
});

test("an empty table has no cursor", () => {
  assert.equal(nextRowIndex(null, "j", 0), null);
  assert.equal(nextRowIndex(2, "k", 0), null);
});

test("a chorded press is left alone", () => {
  assert.ok(ignoreTableKey(null, { ...plain, metaKey: true }));
  assert.ok(ignoreTableKey(null, { ...plain, ctrlKey: true }));
  assert.ok(ignoreTableKey(null, { ...plain, altKey: true }));
  assert.ok(!ignoreTableKey(null, plain));
});
