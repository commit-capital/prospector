import assert from "node:assert/strict";
import { test } from "node:test";
import { autonomyPosture } from "./autonomyLabel.ts";
import type { WorkerFlags } from "./api.ts";

const flags = (overrides: WorkerFlags = {}): WorkerFlags => ({
  TRIAGE_VERIFY_WORKER: "",
  TRIAGE_VERIFY_AUTOHUNT: "",
  TRIAGE_FIX_WORKER: "",
  TRIAGE_FIX_AUTOHUNT: "",
  TRIAGE_FIX_HUNT_FIX: "",
  TRIAGE_FIX_HUNT_RESOLVE: "",
  TRIAGE_FIX_AUTOPUSH: "",
  TRIAGE_WORKER_ID: "",
  ...overrides,
});

test("a machine with no lanes on reads manual", () => {
  const p = autonomyPosture(flags());
  assert.equal(p.label, "manual");
  assert.deepEqual(p.detail, ["No automated work runs on this machine."]);
});

test("autopush parts become header nouns in Setup's order", () => {
  const p = autonomyPosture(flags({ TRIAGE_FIX_AUTOPUSH: "update,rebase,resolve,fix" }));
  assert.equal(p.label, "autonomous: rebases, conflicts, fixes");
});

test("update and rebase are one Setup switch, so one noun", () => {
  assert.equal(autonomyPosture(flags({ TRIAGE_FIX_AUTOPUSH: "update,rebase" })).label,
    "autonomous: rebases");
  assert.equal(autonomyPosture(flags({ TRIAGE_FIX_AUTOPUSH: "update" })).label,
    "autonomous: rebases");
});

test("describe reads as descriptions", () => {
  assert.equal(autonomyPosture(flags({ TRIAGE_FIX_AUTOPUSH: "describe" })).label,
    "autonomous: descriptions");
});

test("lanes on without autopush read as drafts only", () => {
  const p = autonomyPosture(flags({ TRIAGE_VERIFY_WORKER: "1", TRIAGE_FIX_HUNT_FIX: "1" }));
  assert.equal(p.label, "autonomous: drafts only");
  assert.equal(p.detail.length, 2);
});

test("every active behavior contributes one detail sentence", () => {
  const p = autonomyPosture(flags({
    TRIAGE_FIX_AUTOPUSH: "update,rebase",
    TRIAGE_VERIFY_WORKER: "1",
    TRIAGE_VERIFY_AUTOHUNT: "1",
    TRIAGE_FIX_WORKER: "1",
  }));
  assert.equal(p.detail.length, 4);
  assert.ok(p.detail[0].includes("without asking"));
});

test("the worker id is a name, never a behavior", () => {
  assert.equal(autonomyPosture(flags({ TRIAGE_WORKER_ID: "studio-1" })).label, "manual");
});
