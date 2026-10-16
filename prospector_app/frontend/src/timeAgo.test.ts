import assert from "node:assert/strict";
import { test } from "node:test";
import { shortDateTime, timeAgo } from "./timeAgo.ts";

const ago = (days: number): string =>
  new Date(Date.now() - days * 86_400_000).toISOString();

test("missing or unparsable stamps read as a dash", () => {
  assert.equal(timeAgo(null), "—");
  assert.equal(timeAgo(undefined), "—");
  assert.equal(timeAgo("not a date"), "—");
});

test("each unit takes over where the previous ends", () => {
  assert.equal(timeAgo(new Date().toISOString()), "just now");
  assert.equal(timeAgo(ago(1 / 24 / 2)), "30m");
  assert.equal(timeAgo(ago(0.5)), "12h");
  assert.equal(timeAgo(ago(3)), "3d");
  assert.equal(timeAgo(ago(28)), "4w");
});

test("older stamps stay relative instead of flipping to a date", () => {
  assert.equal(timeAgo(ago(120)), "3mo");
  assert.equal(timeAgo(ago(800)), "2y");
});

test("shortDateTime adds the year only outside the current one", () => {
  const now = new Date(2026, 9, 16, 12, 0);
  const sameYear = shortDateTime(new Date(2026, 8, 10, 21, 42).toISOString(), now);
  assert.match(sameYear, /Sep/);
  assert.match(sameYear, /10/);
  assert.doesNotMatch(sameYear, /2026/);
  assert.match(shortDateTime(new Date(2025, 8, 10, 21, 42).toISOString(), now), /2025/);
});

test("shortDateTime is empty for a missing or unparsable stamp", () => {
  assert.equal(shortDateTime(null), "");
  assert.equal(shortDateTime("not a date"), "");
});
