import assert from "node:assert/strict";
import { test } from "node:test";
import { slackHookProblem } from "./slackHook.ts";

test("a Slack incoming webhook URL is accepted", () => {
  assert.equal(slackHookProblem("https://hooks.slack.com/services/T0/B0/abc"), null);
  assert.equal(slackHookProblem("  https://hooks.slack.com/services/T0/B0/abc  "), null);
});

test("anything else is named as the problem before it is saved", () => {
  for (const url of ["", "http://hooks.slack.com/services/x", "https://hooks.slack.com.evil.example/x",
                     "https://example.com/hooks.slack.com/"]) {
    assert.match(slackHookProblem(url) ?? "", /hooks\.slack\.com/);
  }
});
