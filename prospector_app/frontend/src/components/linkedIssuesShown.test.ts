import assert from "node:assert/strict";
import { test } from "node:test";
import type { IssueLink } from "../api";
import { FIX_EVIDENCE, shownIssues } from "./linkedIssuesShown.ts";

function link(issue: number, how: string): IssueLink {
  return { issue, pain: null, how };
}

test("only fix-evidence links are shown", () => {
  const issues = [link(1, "explicit"), link(2, "mention"), link(3, "issue-ref")];
  const { shown, hidden } = shownIssues(issues);
  assert.deepEqual(shown.map((i) => i.issue), [1, 3]);
  assert.equal(hidden, 0);
});

test("every evidence kind carries a tooltip line", () => {
  for (const how of Object.keys(FIX_EVIDENCE)) {
    const { shown } = shownIssues([link(9, how)]);
    assert.equal(shown.length, 1);
    assert.ok(FIX_EVIDENCE[how].length > 0);
  }
});

test("limit collapses the tail into the hidden count", () => {
  const issues = [link(1, "explicit"), link(2, "explicit"), link(3, "fix-found"), link(4, "issue-ref")];
  const { shown, hidden } = shownIssues(issues, 2);
  assert.deepEqual(shown.map((i) => i.issue), [1, 2]);
  assert.equal(hidden, 2);
});

test("a limit at or above the fix-link count hides nothing", () => {
  const issues = [link(1, "explicit"), link(2, "fix-found")];
  assert.deepEqual(shownIssues(issues, 2), { shown: issues, hidden: 0 });
  assert.deepEqual(shownIssues(issues, 5), { shown: issues, hidden: 0 });
});

test("no limit shows every fix link", () => {
  const issues = Array.from({ length: 12 }, (_, i) => link(i + 1, "explicit"));
  const { shown, hidden } = shownIssues(issues);
  assert.equal(shown.length, 12);
  assert.equal(hidden, 0);
});

test("undefined and evidence-free lists are empty", () => {
  assert.deepEqual(shownIssues(undefined, 3), { shown: [], hidden: 0 });
  assert.deepEqual(shownIssues([link(1, "mention")], 3), { shown: [], hidden: 0 });
});
