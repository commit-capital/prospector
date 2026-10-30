import assert from "node:assert/strict";
import { test } from "node:test";
import { moveKey, orderColumns, widthCss } from "./columnLayout.ts";

const cols = ["a", "b", "c", "d"].map((key) => ({ key }));
const keys = (cs: { key: string }[]) => cs.map((c) => c.key);

test("columns follow the saved order", () => {
  assert.deepEqual(keys(orderColumns(cols, ["c", "a", "d", "b"])), ["c", "a", "d", "b"]);
});

test("a column the saved order lacks sits after its default predecessor", () => {
  assert.deepEqual(keys(orderColumns(cols, ["d", "b", "a"])), ["d", "b", "c", "a"]);
  assert.deepEqual(keys(orderColumns(cols, ["c", "b"])), ["a", "c", "d", "b"]);
});

test("unknown saved keys are dropped", () => {
  assert.deepEqual(keys(orderColumns(cols, ["gone", "b"])), ["a", "b", "c", "d"]);
});

test("dragging right lands after the target, left lands before it", () => {
  assert.deepEqual(moveKey(["a", "b", "c", "d"], "a", "c"), ["b", "c", "a", "d"]);
  assert.deepEqual(moveKey(["a", "b", "c", "d"], "d", "b"), ["a", "d", "b", "c"]);
});

test("a drop on itself or an unknown key changes nothing", () => {
  assert.deepEqual(moveKey(["a", "b"], "a", "a"), ["a", "b"]);
  assert.deepEqual(moveKey(["a", "b"], "x", "a"), ["a", "b"]);
});

test("width CSS pins sized columns and clamps to the minimum", () => {
  const css = widthCss("pr-table", { title: 300.4, pain: 5 });
  assert.match(css, /\.pr-table :is\(th, td\)\.c-title \{ width: 300px; min-width: 300px; max-width: 300px;/);
  assert.match(css, /\.c-pain \{ width: 40px;/);
});

test("width CSS skips keys that are not plain identifiers", () => {
  assert.equal(widthCss("t", { "a}b": 100 }), "");
});
