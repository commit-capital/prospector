import assert from "node:assert/strict";
import { test } from "node:test";
import type { AutohuntRun } from "../../api";
import { countResults, mergeRuns } from "./runHistory.ts";

const run = (phase: AutohuntRun["phase"], pr: number, finished: string | null, result: string | null = null,
  started: string | null = null): AutohuntRun => ({ phase, pr, finished, started, result });

test("merges both histories newest first", () => {
  const merged = mergeRuns(
    [run("security", 1, "2026-10-03T10:00:00Z"), run("verify", 2, "2026-10-03T08:00:00Z")],
    [run("fix", 3, "2026-10-03T09:00:00Z"), run("fix", 4, null, null, "2026-10-03T11:00:00Z")]);
  assert.deepEqual(merged.map((r) => r.pr), [4, 1, 3, 2]);
});

test("counts each lane's results", () => {
  const counts = countResults([run("fix", 1, null, "failed"), run("fix", 2, null, "failed"),
    run("security", 3, null, "GREEN"), run("verify", 4, null, null)]);
  assert.deepEqual(counts, { security: { GREEN: 1 }, verify: { "—": 1 }, fix: { failed: 2 } });
});
