import assert from "node:assert/strict";
import { test } from "node:test";
import { edge, step, typeahead, type SelectItem } from "./selectNav.ts";

const item = (label: string, disabled = false): SelectItem => ({ value: label, label, disabled, group: null });
const items = [item("merge"), item("approve", true), item("comment"), item("close — stale"), item("close — dup of", true)];

test("stepping skips disabled items and stops at either end", () => {
  assert.equal(step(items, 0, 1), 2);
  assert.equal(step(items, 2, -1), 0);
  assert.equal(step(items, 0, -1), 0);
  assert.equal(step(items, 3, 1), 3);
  assert.equal(step(items, -1, 1), 0);
});

test("the edges are the first and last enabled items", () => {
  assert.equal(edge(items, "first"), 0);
  assert.equal(edge(items, "last"), 3);
  assert.equal(edge([item("x", true)], "first"), -1);
});

test("typeahead matches a label prefix after the active item, wrapping", () => {
  assert.equal(typeahead(items, "c", 0), 2);
  assert.equal(typeahead(items, "c", 2), 3);
  assert.equal(typeahead(items, "c", 3), 2);
  assert.equal(typeahead(items, "CLOSE", 0), 3);
  assert.equal(typeahead(items, "a", 0), -1);
});
