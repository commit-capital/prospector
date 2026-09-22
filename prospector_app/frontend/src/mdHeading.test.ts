import assert from "node:assert/strict";
import { test } from "node:test";
import { stripMdHeading } from "./mdHeading.ts";

test("a leading heading marker is dropped", () => {
  assert.equal(stripMdHeading("# OS Command Injection in fs-git"), "OS Command Injection in fs-git");
  assert.equal(stripMdHeading("### deep heading"), "deep heading");
  assert.equal(stripMdHeading("  ## indented"), "indented");
});

test("a missing summary passes through as null", () => {
  assert.equal(stripMdHeading(null), null);
  assert.equal(stripMdHeading(undefined), null);
});

test("text without a heading marker is untouched", () => {
  assert.equal(stripMdHeading("plain summary"), "plain summary");
  assert.equal(stripMdHeading("#1234 looks like an issue ref"), "#1234 looks like an issue ref");
  assert.equal(stripMdHeading("mid # hash stays"), "mid # hash stays");
});
