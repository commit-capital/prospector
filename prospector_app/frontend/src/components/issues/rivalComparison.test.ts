import assert from "node:assert/strict";
import { test } from "node:test";
import type { IssueDetail } from "../../api";
import { otherOpenPRs, rivalRows } from "./rivalComparison.ts";

const issue = (run: object | null): IssueDetail => ({
  number: 14867,
  fix_run: run,
  linked_prs: [
    { pr: 15440, how: "explicit", state: "open" },
    { pr: 15500, how: "explicit", state: "open" },
    { pr: 15600, how: "explicit", state: "closed" },
    { pr: 360, how: "subsystem", state: "open" },
    { pr: 15700, how: "github", state: "open" },
  ],
} as unknown as IssueDetail);

const covered = { pr: 15440, author: "contrib", verdict: "covered", why: "our fix passes its tests" };

test("our fix lines up against every other open PR that references the issue", () => {
  const rows = rivalRows(issue({ ending: "fixed", proposal: { pr: 15700, url: null },
                                 second_opinion: [covered] }));
  assert.deepEqual(rows, [{ pr: 15440, entry: covered }, { pr: 15500, entry: null }]);
});

test("no comparison until we have a fix", () => {
  assert.deepEqual(rivalRows(issue(null)), []);
  assert.deepEqual(rivalRows(issue({ ending: "fix-disputed" })), []);
});

test("the PRs to go with leave out ours and any Prospector opened", () => {
  const d = { ...issue({ ending: "fixed", proposal: { pr: 15700, url: null } }),
    linked_prs: [{ pr: 15440, how: "explicit", state: "open" },
                 { pr: 15800, how: "explicit", state: "open", facts: { author_kind: "prospector" } }] };
  assert.deepEqual(otherOpenPRs(d as unknown as IssueDetail), [15440]);
});
