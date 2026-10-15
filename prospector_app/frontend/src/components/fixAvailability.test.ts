import assert from "node:assert/strict";
import { test } from "node:test";
import type { ReviewFinding } from "../api";
import { designFixGoal } from "./fixAvailability.ts";

function finding(over: Partial<ReviewFinding>): ReviewFinding {
  return { path: "src/a.ts", line: 3, severity: "minor", title: "dup helper", body: "b",
           resolved: false, outdated: false, commit: null, url: null, ...over };
}

test("the goal lists every open finding its verifier did not refute", () => {
  const goal = designFixGoal([
    finding({}),
    finding({ path: "src/b.ts", line: null, severity: "major", title: "wrong layer",
              verification: { verdict: "confirmed", reason: "" } }),
    finding({ title: "refuted", verification: { verdict: "refuted", reason: "no" } }),
    finding({ title: "resolved", resolved: true }),
  ]);
  assert.equal(goal, "Fix these design review findings, and nothing else:\n"
    + "- src/a.ts:3 (minor) dup helper\n- src/b.ts (major) wrong layer");
});

test("no open finding leaves no goal", () => {
  assert.equal(designFixGoal([finding({ outdated: true })]), "");
});
