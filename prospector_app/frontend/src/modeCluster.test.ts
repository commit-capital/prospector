import assert from "node:assert/strict";
import { test } from "node:test";
import { autonomyLabel, identityLabel } from "./modeCluster.ts";

test("active autopush actions read as their nouns, in display order", () => {
  assert.equal(autonomyLabel(["resolve", "update", "rebase"]),
    "autonomous: updates, rebases, conflicts");
});

test("an agent-authored fix pushed unattended reads as fixes", () => {
  assert.equal(autonomyLabel(["fix"]), "autonomous: fixes");
});

test("an empty policy reads as asks first", () => {
  assert.equal(autonomyLabel([]), "asks first");
});

test("the bot alone when no push identity is configured", () => {
  assert.equal(identityLabel("commitperclip-bot", null), "as commitperclip-bot");
});

test("both identities when the push user is distinct", () => {
  assert.equal(identityLabel("commitperclip-bot", "commitperclip"),
    "as commitperclip-bot + commitperclip");
});

test("one name when the two logins coincide", () => {
  assert.equal(identityLabel("commitperclip-bot", "commitperclip-bot"),
    "as commitperclip-bot");
});
