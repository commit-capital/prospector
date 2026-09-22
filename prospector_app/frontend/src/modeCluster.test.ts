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
    modePolicyLabel(["update", "rebase", "resolve"], "commitperclip-bot", "commitperclip-bot"),
    "autonomous: branch updates, conflicts · as commitperclip-bot");
});

test("modePolicyLabel names the push account when it differs from the bot", () => {
  assert.equal(
    modePolicyLabel(["resolve"], "commitperclip-bot", "commitperclip"),
    "autonomous: conflicts · as commitperclip-bot · pushes as commitperclip");
});

test("modePolicyLabel with no autonomous pushes says the system asks first", () => {
  assert.equal(
    modePolicyLabel([], "commitperclip-bot", "commitperclip"),
    "asks before pushing · as commitperclip-bot");
});

test("modePolicyTitle spells out both lanes", () => {
  const title = modePolicyTitle(["update"], "bot", "pusher", "acme/widgets");
  assert.match(title, /posted as bot/);
  assert.match(title, /branch updates are pushed to contributors' PR branches without asking, as pusher/);
  const idle = modePolicyTitle([], "bot", null, null);
  assert.match(idle, /waits for a person's approval/);
});
