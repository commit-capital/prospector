import assert from "node:assert/strict";
import { test } from "node:test";
import type { FlaggedPr, ThreatSummary } from "./api.ts";
import { threatBannerParts, threatsHref } from "./threatBanner.ts";

function flagged(pr: number): FlaggedPr {
  return { pr, title: `PR ${pr}`, author: "mallory", url: null, verdict: "malicious",
           signatures: ["obfuscated-payload"], noticed: "2026-10-02" };
}

const quiet: ThreatSummary = { malicious: [], suspicious: 0, secrets: [] };

function texts(threats: ThreatSummary): string[] | undefined {
  return threatBannerParts(threats)?.map((p) => p.text);
}

test("a quiet store raises no banner", () => {
  assert.equal(threatBannerParts(quiet), null);
  assert.equal(threatBannerParts(undefined), null);
});

test("suspicious PRs alone raise no banner", () => {
  assert.equal(threatBannerParts({ ...quiet, suspicious: 4 }), null);
});

test("the banner counts the malicious PRs and names each one", () => {
  assert.deepEqual(texts({ ...quiet, malicious: [11987, 11988, 12035].map(flagged) }),
                   ["3 open PRs flagged malicious: #11987, #11988, #12035"]);
});

test("a long list names the first six and counts the rest", () => {
  assert.deepEqual(texts({ ...quiet, malicious: [1, 2, 3, 4, 5, 6, 7, 8].map(flagged) }),
                   ["8 open PRs flagged malicious: #1, #2, #3, #4, #5, #6 +2"]);
});

test("a maintainer's leaked credential raises the banner on its own and names the PR", () => {
  assert.deepEqual(texts({ ...quiet, secrets: [13575] }),
                   ["1 credential leaked in a maintainer's PR: #13575"]);
  assert.deepEqual(texts({ malicious: [flagged(9)], suspicious: 0, secrets: [4, 7] }),
                   ["1 open PR flagged malicious: #9", "2 credentials leaked in maintainers' PRs: #4, #7"]);
});

test("each phrase opens the Threats view narrowed to what it counted", () => {
  const parts = threatBannerParts({ malicious: [flagged(9)], suspicious: 0, secrets: [4] });
  assert.deepEqual(parts?.map((p) => threatsHref(p.focus)),
                   ["/security/threats?show=malicious", "/security/threats?show=credentials"]);
});
