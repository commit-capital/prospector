import assert from "node:assert/strict";
import { test } from "node:test";
import { actionsFor } from "./prStatusActions.ts";

test("each state offers the actions that fit it", () => {
  assert.deepEqual(actionsFor("moving", "security"), ["run-now"]);
  assert.deepEqual(actionsFor("moving", "fix"), []);
  assert.deepEqual(actionsFor("moving", null), []);
  assert.deepEqual(actionsFor("waiting", null), []);
  assert.deepEqual(actionsFor("ready", null), ["merge"]);
  assert.deepEqual(actionsFor("needs-you", null), ["quick-click"]);
  assert.deepEqual(actionsFor("stalled", null), ["ask-agent", "re-attempt", "accept", "abandon"]);
});

test("only a stalled PR is offered the agent or abandon", () => {
  for (const s of ["moving", "waiting", "ready", "needs-you"] as const) {
    const got = actionsFor(s, "design");
    assert.ok(!got.includes("ask-agent") && !got.includes("abandon"), s);
  }
});
