import assert from "node:assert/strict";
import { test } from "node:test";
import type { FlaggedPr, ThreatSummary } from "./api.ts";
import { threatBannerParts } from "./threatBanner.ts";

function flagged(pr: number): FlaggedPr {
  return { pr, title: `PR ${pr}`, author: "mallory", url: null, verdict: "malicious",
           signatures: ["obfuscated-payload"], noticed: "2026-10-02" };
}

const quiet: ThreatSummary = { malicious: [], suspicious: 0, secrets: 0 };

test("a quiet store raises no banner", () => {
  assert.equal(threatBannerParts(quiet), null);
  assert.equal(threatBannerParts(undefined), null);
});

test("suspicious PRs alone raise no banner", () => {
  assert.equal(threatBannerParts({ ...quiet, suspicious: 4 }), null);
});

test("the banner counts the malicious PRs and names each one", () => {
  const parts = threatBannerParts({ ...quiet, malicious: [11987, 11988, 12035].map(flagged) });
  assert.deepEqual(parts, ["3 open PRs flagged malicious: #11987, #11988, #12035"]);
});

test("a long list names the first six and counts the rest", () => {
  const parts = threatBannerParts({ ...quiet, malicious: [1, 2, 3, 4, 5, 6, 7, 8].map(flagged) });
  assert.deepEqual(parts, ["8 open PRs flagged malicious: #1, #2, #3, #4, #5, #6 +2"]);
});

test("credentials to rotate raise the banner on their own, singular when one", () => {
  assert.deepEqual(threatBannerParts({ ...quiet, secrets: 1 }), ["1 leaked credential to rotate"]);
  assert.deepEqual(threatBannerParts({ malicious: [flagged(9)], suspicious: 0, secrets: 2 }),
                   ["1 open PR flagged malicious: #9", "2 leaked credentials to rotate"]);
});
