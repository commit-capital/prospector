import assert from "node:assert/strict";
import { test } from "node:test";
import { timeAgo } from "./timeAgo.ts";

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
