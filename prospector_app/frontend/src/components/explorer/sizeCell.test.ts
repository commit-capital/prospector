import assert from "node:assert/strict";
import { test } from "node:test";
import { sizeCell } from "./sizeCell.ts";

test("loc and files read as one compact cell", () => {
  assert.deepEqual(sizeCell(239, 250, 8), { text: "+239 · 8f", noisy: false });
});

test("a noisy diff keeps its effective/raw pair", () => {
  assert.deepEqual(sizeCell(12, 400, 3), { text: "12/400 · 3f", noisy: true });
});

test("a missing half drops out", () => {
  assert.deepEqual(sizeCell(5, 5, null), { text: "+5", noisy: false });
  assert.deepEqual(sizeCell(null, null, 4), { text: "4f", noisy: false });
});

test("nothing known reads as a dash", () => {
  assert.deepEqual(sizeCell(null, null, null), { text: "—", noisy: false });
});
