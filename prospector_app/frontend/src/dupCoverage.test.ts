import assert from "node:assert/strict";
import { test } from "node:test";
import { coverageLabel, coverageTone } from "./dupCoverage.ts";

test("tones map coverage to chip colors", () => {
  assert.equal(coverageTone("landed"), "green");
  assert.equal(coverageTone("pr"), "blue");
  assert.equal(coverageTone("unique"), "red");
});

test("labels name the covering commit or PR", () => {
  assert.equal(coverageLabel({ coverage: "landed", landed_sha: "abc1234def" }), "landed abc1234");
  assert.equal(coverageLabel({ coverage: "landed" }), "landed");
  assert.equal(coverageLabel({ coverage: "pr", covered_by: 42 }), "PR #42");
  assert.equal(coverageLabel({ coverage: "pr" }), "another PR");
  assert.equal(coverageLabel({ coverage: "unique" }), "unique");
});
