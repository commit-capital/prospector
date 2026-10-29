import assert from "node:assert/strict";
import { test } from "node:test";
import { stateLabel, stateTone } from "./prStatus.ts";

test("each state reads as its own word", () => {
  assert.deepEqual(
    (["moving", "waiting", "ready", "needs-you", "stalled"] as const).map(stateLabel),
    ["Moving", "Waiting", "Ready", "Needs you", "Stalled"]);
});

test("each state takes the chip tone the status block uses", () => {
  assert.deepEqual(
    (["moving", "waiting", "ready", "needs-you", "stalled"] as const).map(stateTone),
    ["blue", "muted", "green", "yellow", "red"]);
});
