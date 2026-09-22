import assert from "node:assert/strict";
import { test } from "node:test";
import { sparklinePath, staleFromX } from "./sparkline.ts";

test("a rising series spans the box left to right, top value highest", () => {
  const path = sparklinePath([0, 5, 10], 100, 30, 2);
  assert.equal(path, "M 2.0,28.0 L 50.0,15.0 L 98.0,2.0");
});

test("a flat series draws at mid-height", () => {
  const path = sparklinePath([3, 3], 100, 30, 2);
  assert.equal(path, "M 2.0,15.0 L 98.0,15.0");
});

test("nulls break the line into segments", () => {
  const path = sparklinePath([0, null, 10], 100, 30, 2);
  assert.equal(path, "M 2.0,28.0 M 98.0,2.0");
});

test("an all-null or empty series draws nothing", () => {
  assert.equal(sparklinePath([null, null], 100, 30), "");
  assert.equal(sparklinePath([], 100, 30), "");
});

test("staleFromX starts shading the day after the last ingest", () => {
  const days = ["2026-06-20", "2026-06-21", "2026-06-22"];
  // Ingest during the middle day: only the last day is stale.
  const x = staleFromX(days, "2026-06-21T10:00:00", 100, 2);
  assert.equal(x, 98);
});

test("staleFromX is null when the ingest covers the newest day", () => {
  const days = ["2026-06-21", "2026-06-22"];
  assert.equal(staleFromX(days, "2026-06-22T01:00:00", 100), null);
});

test("staleFromX shades everything when no ingest ever ran", () => {
  const days = ["2026-06-21", "2026-06-22"];
  assert.equal(staleFromX(days, null, 100, 2), 2);
  assert.equal(staleFromX([], null, 100), null);
});
