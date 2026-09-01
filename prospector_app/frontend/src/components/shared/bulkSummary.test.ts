import assert from "node:assert/strict";
import { test } from "node:test";
import { chipTone, countStatuses, summaryLine } from "./bulkSummary.ts";

test("countStatuses tallies in first-seen order and summaryLine joins them", () => {
  const counts = countStatuses(["executed", "skipped", "executed", "error"]);
  assert.deepEqual(counts, { executed: 2, skipped: 1, error: 1 });
  assert.equal(summaryLine(counts), "2 executed · 1 skipped · 1 error");
});

test("chipTone maps landed, failed, and in-flight statuses", () => {
  assert.equal(chipTone("executed"), "green");
  assert.equal(chipTone("blocked"), "red");
  assert.equal(chipTone("running"), "blue");
  assert.equal(chipTone("dry-run"), "muted");
});
