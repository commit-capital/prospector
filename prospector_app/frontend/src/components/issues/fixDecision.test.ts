import assert from "node:assert/strict";
import { test } from "node:test";
import type { IssueDetail } from "../../api";
import { fixDecision, sendBackDraft } from "./fixDecision.ts";

const issue = (extra: Partial<IssueDetail> = {}): IssueDetail => ({
  number: 14777, state: "open", fix_status: "review", fix_pr: 15363,
  analysis: { disposition: "needs-human",
              rationale: "It only adds logging. It does not fix the no-op cancel." },
  ...extra,
} as IssueDetail);

test("an open fix PR waiting on your review asks approve or full fix", () => {
  assert.deepEqual(fixDecision(issue()), { pr: 15363, partial: true });
});

test("no decision without an open PR, while a request runs, once approved, or on a closed issue", () => {
  assert.equal(fixDecision(issue({ fix_pr: null })), null);
  assert.equal(fixDecision(issue({ fix_status: "pr-open" })), null);
  assert.equal(fixDecision(issue({ state: "closed" })), null);
  assert.equal(fixDecision(issue({ fix_request: { action: "send-back", status: "running",
                                                  source: "operator" } })), null);
});

test("approving defaults to a partial fix only when something says it is one", () => {
  assert.equal(fixDecision(issue({ analysis: { disposition: "link-pr" } }))?.partial, false);
  assert.equal(fixDecision(issue({ analysis: { disposition: "link-pr" }, fix_partial: true }))?.partial,
    true);
});

test("the send-back draft carries the reviewer's reasons and what the fix leaves", () => {
  const draft = sendBackDraft(issue({ fix_run: { reviews: [{ lens: "scope-safety", concerns: [],
    coverage: [{ defect: "cancel is a no-op", status: "not-addressed", evidence: "" },
               { defect: "the error is swallowed", status: "addressed", evidence: "" }] }] } as
    unknown as IssueDetail["fix_run"] }));
  assert.match(draft, /#15363 doesn't fix all of #14777/);
  assert.match(draft, /It only adds logging/);
  assert.match(draft, /- cancel is a no-op/);
  assert.doesNotMatch(draft, /the error is swallowed/);
});

test("a draft with nothing from the reviewer still asks for the whole report", () => {
  assert.match(sendBackDraft(issue({ analysis: null })), /every bug #14777 reports/);
});
