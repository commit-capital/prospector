import assert from "node:assert/strict";
import { test } from "node:test";
import type { SafetyFinding } from "../api";
import { bannerFindings } from "./bannerFindings.ts";

function finding(severity: string, title: string): SafetyFinding {
  return { severity, title, lens: "l", category: "c", detail: "d", location: "a.ts:1" };
}

test("the banner names the most severe findings first", () => {
  const { shown, more } = bannerFindings([
    finding("low", "a"), finding("HIGH", "b"), finding("medium", "c"), finding("odd", "d"),
  ]);
  assert.deepEqual(shown.map((f) => f.title), ["b", "c", "a"]);
  assert.equal(more, 1);
});

test("a short list is shown whole", () => {
  const { shown, more } = bannerFindings([finding("medium", "only")]);
  assert.deepEqual(shown.map((f) => f.title), ["only"]);
  assert.equal(more, 0);
});
