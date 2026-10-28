import assert from "node:assert/strict";
import { test } from "node:test";
import type { IssueDetail } from "../../api";
import { holdReasons, testFailures } from "./holdReasons.ts";

const issue = (extra: object): IssueDetail => ({ number: 14149, fix_pr: null, ...extra } as unknown as IssueDetail);
const defect = (d: string, status: string) => ({ defect: d, status, evidence: "" });

test("a parked fix lists every hold, then that it is partial, with how much it handles", () => {
  const d = issue({
    fix_holds: [{ kind: "rival", text: "another open pull request covers what this fix misses: #14150" },
                { kind: "boundary", text: "it crosses a trust boundary" }],
    fix_run: { boundary: { crossings: [{ kind: "auth", where: "a", what: "w", requested: true }] } },
    fix_partial: true,
    fix_coverage: { state: "partial", unshown: [], defects: [
      defect("deltas are trimmed", "addressed"), defect("whitespace deltas dropped", "already-fixed"),
      defect("stored comments can't be repaired", "not-addressed")] },
  });
  assert.deepEqual(holdReasons(d), [
    { kind: "rival", text: "Another open PR covers what ours misses" },
    { kind: "boundary", text: "It crosses a trust boundary" },
    { kind: "partial", text: "Partial fix: 2 of 3 reported defects handled" },
  ]);
});

test("a fix PR handed back leads, and a green one has nothing held", () => {
  assert.deepEqual(holdReasons(issue({ fix_pr: 15363, fix_followup: { pr: 15363, state: "handed-back" } })),
    [{ kind: "handed-back", text: "#15363 came back to you" }]);
  assert.deepEqual(holdReasons(issue({ fix_pr: 15363, fix_followup: { pr: 15363, state: "ready" } })), []);
});

test("a boundary hold with no crossings says the review gave no answer", () => {
  assert.equal(holdReasons(issue({ fix_holds: [{ kind: "boundary", text: "the trust-boundary review did not finish" }],
                                   fix_run: { boundary: { failed: true, crossings: [] } } }))[0].text,
    "The trust-boundary review gave no answer");
});

// #14149's rival #14150, run against our fix (vitest, with its colour codes).
const VITEST = [
  "\x1b[41m\x1b[1m FAIL \x1b[22m\x1b[49m \x1b[30m\x1b[46m @paperclipai/adapter-openclaw-gateway \x1b[49m\x1b[39m src/server/execute.test.ts\x1b[2m > \x1b[22mreadAssistantChunk\x1b[2m > \x1b[22mkeeps whitespace",
  "\x1b[31m\x1b[1mTypeError\x1b[22m: readAssistantChunk is not a function\x1b[39m",
  "    135|       readAssistantChunk({ delta }),",
  "\x1b[41m\x1b[1m FAIL \x1b[22m\x1b[49m src/server/execute.test.ts > readAssistantChunk > falls back to text",
  "TypeError: readAssistantChunk is not a function",
  "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[31m2 failed\x1b[39m\x1b[22m\x1b[2m | \x1b[22m\x1b[1m\x1b[32m10 passed\x1b[39m\x1b[22m\x1b[90m (12)\x1b[39m",
].join("\n");

test("a vitest run reads as its failing tests, its distinct errors and its count", () => {
  const f = testFailures(VITEST);
  assert.deepEqual(f.tests, ["src/server/execute.test.ts > readAssistantChunk > keeps whitespace",
                             "src/server/execute.test.ts > readAssistantChunk > falls back to text"]);
  assert.deepEqual(f.errors, ["TypeError: readAssistantChunk is not a function"]);
  assert.equal(f.summary, "2 failed | 10 passed (12)");
});

test("a pytest run reads the same way", () => {
  const f = testFailures([
    "E       AssertionError: assert 1 == 2",
    "FAILED tests/test_x.py::test_y - AssertionError: assert 1 == 2",
    "========== 1 failed, 4 passed in 0.31s ==========",
  ].join("\n"));
  assert.deepEqual(f.tests, ["tests/test_x.py::test_y - AssertionError: assert 1 == 2"]);
  assert.deepEqual(f.errors, ["AssertionError: assert 1 == 2"]);
  assert.equal(f.summary, "1 failed, 4 passed in 0.31s");
});

test("jest's tick list stands in when there are no FAIL lines, and the tail when nothing reads", () => {
  assert.deepEqual(testFailures("  ✓ keeps it (2 ms)\n  ✕ drops it (5 ms)").tests, ["drops it"]);
  const f = testFailures("Segmentation fault\n\nexit 139");
  assert.deepEqual([f.tests, f.errors, f.summary, f.tail], [[], [], null, ["Segmentation fault", "exit 139"]]);
});
