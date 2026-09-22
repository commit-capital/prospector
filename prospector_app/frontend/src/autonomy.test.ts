import assert from "node:assert/strict";
import { test } from "node:test";
import {
  AUTOPUSH_ORDER, autopushSummary, isOn, SWITCHES, switchId,
} from "./autonomy.ts";

test("switch ids are unique", () => {
  const ids = SWITCHES.map(switchId);
  assert.equal(new Set(ids).size, ids.length);
});

test("every part a switch owns is in the autopush ordering", () => {
  for (const s of SWITCHES) {
    for (const p of s.parts ?? []) {
      assert.ok(AUTOPUSH_ORDER.includes(p), `${switchId(s)} owns unknown part ${p}`);
    }
  }
});

test("a parts switch is on only when every part it owns is present", () => {
  const updates = SWITCHES.find((s) => switchId(s) === "TRIAGE_FIX_AUTOPUSH")!;
  const resolve = SWITCHES.find((s) => switchId(s) === "TRIAGE_FIX_AUTOPUSH:resolve")!;
  assert.ok(isOn({ TRIAGE_FIX_AUTOPUSH: "update,rebase" }, updates));
  assert.ok(!isOn({ TRIAGE_FIX_AUTOPUSH: "update" }, updates));
  assert.ok(isOn({ TRIAGE_FIX_AUTOPUSH: "update, rebase , resolve" }, resolve));
  assert.ok(!isOn({}, resolve));
});

test("a plain switch is on whenever its key holds any value", () => {
  const verify = SWITCHES.find((s) => switchId(s) === "TRIAGE_VERIFY_WORKER")!;
  assert.ok(isOn({ TRIAGE_VERIFY_WORKER: "1" }, verify));
  assert.ok(!isOn({ TRIAGE_VERIFY_WORKER: "" }, verify));
  assert.ok(!isOn({}, verify));
});

test("summary names each unattended-push action once, in policy order", () => {
  assert.equal(autopushSummary(["update", "rebase"]), "branch updates");
  assert.equal(autopushSummary(["resolve", "update"]), "branch updates, conflicts");
  assert.equal(
    autopushSummary(["resolve", "describe", "fix", "rebase", "update"]),
    "branch updates, fixes, descriptions, conflicts",
  );
});

test("summary is empty when nothing pushes without approval", () => {
  assert.equal(autopushSummary([]), "");
  assert.equal(autopushSummary(["unknown-action"]), "");
});
