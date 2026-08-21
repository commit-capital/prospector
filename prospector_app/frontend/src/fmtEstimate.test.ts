import assert from "node:assert/strict";
import { test } from "node:test";
import { fmtEstimate } from "./fmtEstimate.ts";

test("no usable duration renders nothing", () => {
  assert.equal(fmtEstimate(null), null);
  assert.equal(fmtEstimate(undefined), null);
  assert.equal(fmtEstimate(0), null);
  assert.equal(fmtEstimate(-5), null);
  assert.equal(fmtEstimate(Number.NaN), null);
});

test("sub-minute durations round to whole seconds, at least one", () => {
  assert.equal(fmtEstimate(0.2), "~1s");
  assert.equal(fmtEstimate(42.4), "~42s");
});

test("minutes show a decimal under ten, whole above", () => {
  assert.equal(fmtEstimate(90), "~1.5m");
  assert.equal(fmtEstimate(60 * 25), "~25m");
});

test("hours show a decimal under ten", () => {
  assert.equal(fmtEstimate(3600 * 1.5), "~1.5h");
});
