import assert from "node:assert/strict";
import { test } from "node:test";
import { unattendedActions, unattendedDetail, unattendedPushes } from "./autonomy.ts";

test("no switches means no unattended actions", () => {
  assert.deepEqual(unattendedActions({}), []);
  assert.deepEqual(unattendedDetail({}), []);
  assert.equal(unattendedPushes({}), false);
});

test("worker lanes alone are not autonomy — they only serve queued clicks", () => {
  const flags = { TRIAGE_VERIFY_WORKER: "1", TRIAGE_FIX_WORKER: "1" };
  assert.deepEqual(unattendedActions(flags), []);
  assert.equal(unattendedPushes(flags), false);
});

test("each hunt switch contributes its noun", () => {
  assert.deepEqual(unattendedActions({ TRIAGE_FIX_AUTOHUNT: "1" }), ["rebases"]);
  assert.deepEqual(unattendedActions({ TRIAGE_FIX_HUNT_RESOLVE: "1" }), ["conflicts"]);
  assert.deepEqual(unattendedActions({ TRIAGE_FIX_HUNT_FIX: "1" }), ["fixes"]);
  assert.deepEqual(unattendedActions({ TRIAGE_VERIFY_AUTOHUNT: "1" }), ["tests"]);
});

test("everything on reads in a stable order", () => {
  const flags = {
    TRIAGE_VERIFY_AUTOHUNT: "1", TRIAGE_FIX_AUTOHUNT: "1",
    TRIAGE_FIX_HUNT_FIX: "1", TRIAGE_FIX_HUNT_RESOLVE: "1",
    TRIAGE_FIX_AUTOPUSH: "update,rebase,resolve",
  };
  assert.deepEqual(unattendedActions(flags), ["rebases", "conflicts", "fixes", "tests"]);
  assert.equal(unattendedPushes(flags), true);
});

test("autopush parts are parsed from the comma list, whitespace tolerated", () => {
  const flags = { TRIAGE_FIX_AUTOPUSH: " update , resolve " };
  assert.deepEqual(unattendedActions(flags), ["rebases", "conflicts"]);
  assert.equal(unattendedPushes(flags), true);
});

test("detail names the push when autopush covers the action, else the hunt", () => {
  assert.deepEqual(unattendedDetail({ TRIAGE_FIX_AUTOPUSH: "update,rebase" }),
    ["pushes branch updates without asking"]);
  assert.deepEqual(unattendedDetail({ TRIAGE_FIX_AUTOHUNT: "1" }),
    ["queues branch updates on its own"]);
  assert.deepEqual(unattendedDetail({ TRIAGE_FIX_HUNT_RESOLVE: "1", TRIAGE_FIX_AUTOPUSH: "resolve" }),
    ["pushes agent-resolved conflicts without asking"]);
});

test("describe in autopush posts as the bot but never pushes a branch", () => {
  const flags = { TRIAGE_FIX_AUTOPUSH: "describe" };
  assert.deepEqual(unattendedActions(flags), ["descriptions"]);
  assert.deepEqual(unattendedDetail(flags), ["posts rewritten PR descriptions without asking"]);
  assert.equal(unattendedPushes(flags), false);
});
