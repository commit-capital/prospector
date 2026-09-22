import assert from "node:assert/strict";
import { test } from "node:test";
import { suggestedAct, suggestedCanonical } from "./prAction.ts";
import type { PRDetail, Suggestion } from "./api.ts";

const pr = (suggestion: Partial<Suggestion> | undefined): PRDetail =>
  ({ number: 1, suggestion } as unknown as PRDetail);

test("no suggestion defaults to comment", () => {
  assert.equal(suggestedAct(pr(undefined)), "COMMENT");
});

test("a clean merge pick selects merge", () => {
  assert.equal(suggestedAct(pr({ action: "MERGE", accept: { kind: "merge" } })), "MERGE");
});

test("a blocked merge pick still selects merge, so the button matches the verdict", () => {
  assert.equal(suggestedAct(pr({ action: "BLOCKED", label: "Merge blocked", accept: null })), "MERGE");
});

test("an unanalyzed PR (no acceptable action) defaults to comment", () => {
  assert.equal(suggestedAct(pr({ action: "ANALYZE", label: "Not yet analyzed", accept: null })), "COMMENT");
});

test("a close pick selects its close action and pre-fills the canonical", () => {
  const p = pr({ action: "CLOSE_DUP", accept: { kind: "close", action: "CLOSE_DUP", canonical: 42 } });
  assert.equal(suggestedAct(p), "CLOSE_DUP");
  assert.equal(suggestedCanonical(p), "42");
});

test("a review pick maps its event", () => {
  assert.equal(suggestedAct(pr({ action: "REQUEST_CHANGES",
    accept: { kind: "review", event: "request-changes" } })), "REQUEST_CHANGES");
  assert.equal(suggestedAct(pr({ action: "APPROVE",
    accept: { kind: "review", event: "approve" } })), "APPROVE");
});
