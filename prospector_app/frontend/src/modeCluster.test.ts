import assert from "node:assert/strict";
import { test } from "node:test";
import { autopushNames, modePolicyLabel, modePolicyTitle } from "./modeCluster.ts";

test("autopushNames merges update/rebase and keeps display order", () => {
  assert.deepEqual(autopushNames(["resolve", "rebase", "update"]), ["branch updates", "conflicts"]);
  assert.deepEqual(autopushNames(["fix", "describe"]), ["fixes", "descriptions"]);
  assert.deepEqual(autopushNames([]), []);
});

test("modePolicyLabel discloses the autonomous actions and the identity", () => {
  assert.equal(
    modePolicyLabel(["update", "rebase", "resolve"], "my-triage-bot", "my-triage-bot"),
    "autonomous: branch updates, conflicts · as my-triage-bot");
});

test("modePolicyLabel names the push account when it differs from the bot", () => {
  assert.equal(
    modePolicyLabel(["resolve"], "my-triage-bot", "my-triage-push"),
    "autonomous: conflicts · as my-triage-bot · pushes as my-triage-push");
});

test("modePolicyLabel with no autonomous pushes says the system asks first", () => {
  assert.equal(
    modePolicyLabel([], "my-triage-bot", "my-triage-push"),
    "asks before pushing · as my-triage-bot");
});

test("modePolicyTitle spells out both lanes", () => {
  const title = modePolicyTitle(["update"], "bot", "pusher", "acme/widgets");
  assert.match(title, /posted as bot/);
  assert.match(title, /branch updates are pushed to contributors' PR branches without asking, as pusher/);
  const idle = modePolicyTitle([], "bot", null, null);
  assert.match(idle, /waits for a person's approval/);
});
