import assert from "node:assert/strict";
import { test } from "node:test";
import { evidenceLine, needsRetry } from "./threatEvidence.ts";
import type { ThreatEvidence } from "./api.ts";

const HEAD = "e".repeat(40);
const BEFORE = "d".repeat(40);

const capture = (over: Partial<ThreatEvidence> = {}): ThreatEvidence => ({
  id: 1, pr: 11987, head_sha: HEAD, author: "mallory", captured_at: "2026-10-02T17:10:00+00:00",
  complete: true, captured_by: "operator", machine: "studio",
  artifacts: { diff: { bytes: 10, sha256: "a".repeat(64), source: "compare" }, prior: null },
  force_pushes: [], signatures: ["obfuscated-self-decoder"], errors: [], ...over,
});

test("a complete capture without a force-push names its head", () => {
  const line = evidenceLine(capture());
  assert.equal(line.state, "complete");
  assert.equal(line.head, "eeeeeee");
  assert.equal(line.replaced, null);
});

test("the force-push that produced the head is reported with the head it replaced", () => {
  const line = evidenceLine(capture({
    complete: false,
    force_pushes: [
      { at: "2026-08-23T21:29:24Z", actor: "mallory", before: null, after: "f".repeat(40) },
      { at: "2026-09-28T16:44:31Z", actor: "mallory", before: BEFORE, after: HEAD },
    ],
  }));
  assert.equal(line.state, "partial");
  assert.deepEqual(line.replaced, { at: "2026-09-28T16:44:31Z", before: "ddddddd" });
});

test("a force-push to some other head is not reported", () => {
  const line = evidenceLine(capture({
    force_pushes: [{ at: "2026-08-23T21:29:24Z", actor: "x", before: BEFORE, after: "f".repeat(40) }],
  }));
  assert.equal(line.replaced, null);
});

test("a prior head found in the diff cache is reported without a push time", () => {
  const line = evidenceLine(capture({
    artifacts: {
      diff: { bytes: 10, sha256: "a".repeat(64), source: "compare" },
      prior: { bytes: 5, sha256: "b".repeat(64), source: "compare", before_sha: BEFORE, found_by: "diff-cache" },
    },
  }));
  assert.deepEqual(line.replaced, { at: null, before: "ddddddd" });
});

test("only captures that are all partial offer a retry", () => {
  assert.equal(needsRetry([capture({ complete: false }), capture({ complete: true })]), false);
  assert.equal(needsRetry([capture({ complete: false })]), true);
  assert.equal(needsRetry([]), true);
});
