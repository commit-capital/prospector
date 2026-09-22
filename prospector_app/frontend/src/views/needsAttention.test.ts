import assert from "node:assert/strict";
import { test } from "node:test";
import { groupNeedsAttention } from "./needsAttention.ts";

test("groups by lane and reason, largest first", () => {
  const groups = groupNeedsAttention(
    [1, 2, 3],
    { "1": "agent timed out", "2": "agent timed out", "3": "agent timed out" },
    [{ pr: 4, error_kind: "base-lane" }, { pr: 5, error_kind: "base-lane" }],
  );
  assert.deepEqual(groups, [
    { lane: "security", reason: "agent timed out", prs: [1, 2, 3] },
    { lane: "verify", reason: "base-lane", prs: [4, 5] },
  ]);
});

test("a missing or blank reason falls back to \"run failed\"", () => {
  const groups = groupNeedsAttention([7], { "7": "  " }, [{ pr: 8 }, { pr: 9, error_kind: null }]);
  assert.deepEqual(groups, [
    { lane: "verify", reason: "run failed", prs: [8, 9] },
    { lane: "security", reason: "run failed", prs: [7] },
  ]);
});

test("the same reason in different lanes stays two groups", () => {
  const groups = groupNeedsAttention([1], { "1": "run failed" }, [{ pr: 2, error_kind: "run failed" }]);
  assert.equal(groups.length, 2);
});
