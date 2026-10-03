import assert from "node:assert/strict";
import { test } from "node:test";
import type { MachineActivity } from "../../api";
import { activityPhrases, machineStatus } from "./activitySentence.ts";

const NOW = Date.parse("2026-10-03T12:00:00Z");

function machine(over: Partial<MachineActivity> = {}): MachineActivity {
  return {
    host: "studio", local: false, online: true, offline_since: null, has_worker: true,
    tripped: [], current: { pr: null, issue: null }, lanes: {}, background: [], jobs: [],
    spend_usd: 0, ...over,
  };
}

test("a lane reads as a count with its outcomes", () => {
  const [p] = activityPhrases(machine({ lanes: { security: { count: 8, numbers: [1, 2], outcomes: [
    { label: "GREEN", count: 4, numbers: [1] }, { label: "YELLOW", count: 4, numbers: [2] }] } } }));
  assert.equal(p.lead.text, "8 security reviews");
  assert.deepEqual(p.lead.numbers, [1, 2]);
  assert.deepEqual(p.parts.map((x) => x.text), ["4 GREEN", "4 YELLOW"]);
  assert.equal(p.style, "paren");
  assert.equal(p.linkKind, "pr");
});

test("one run reads singular", () => {
  const [p] = activityPhrases(machine({ lanes: { issue_fix: { count: 1, numbers: [70], outcomes: [
    { label: "fixed", count: 1, numbers: [70] }] } } }));
  assert.equal(p.lead.text, "1 issue fix");
  assert.equal(p.linkKind, "issue");
});

test("failures read as bad", () => {
  const [p] = activityPhrases(machine({ lanes: { autofix: { count: 3, numbers: [7], outcomes: [
    { label: "failed", count: 1, numbers: [7] }, { label: "pushed", count: 1, numbers: [7] },
    { label: "error: sandbox-error", count: 1, numbers: [7] }] } } }));
  assert.deepEqual(p.parts.map((x) => x.bad ?? false), [true, false, true]);
});

test("lanes come in a fixed order, then background, jobs and spend", () => {
  const lane = { count: 1, numbers: [1], outcomes: [] };
  const keys = activityPhrases(machine({
    lanes: { autofix: lane, security: lane }, background: [{ label: "PR watches", count: 71 }],
    jobs: [{ label: "Ingest", kind: "ingest", status: "done", job_id: null }], spend_usd: 41.3,
  })).map((p) => p.key);
  assert.deepEqual(keys, ["security", "autofix", "background", "jobs", "spend"]);
});

test("background lists its passes", () => {
  const [p] = activityPhrases(machine({ background: [{ label: "PR watches", count: 71 }, { label: "threat scans", count: 60 }] }));
  assert.equal(p.lead.text, "background:");
  assert.deepEqual(p.parts.map((x) => x.text), ["71 PR watches", "60 threat scans"]);
  assert.equal(p.style, "list");
});

test("this machine's jobs read as yours and carry their id", () => {
  const [p] = activityPhrases(machine({ local: true, jobs: [
    { label: "Ingest", kind: "ingest", status: "done", job_id: 3 },
    { label: "Threat scan", kind: "threat-scan", status: "failed", job_id: 4 }] }));
  assert.equal(p.lead.text, "you ran");
  assert.deepEqual(p.parts, [{ text: "Ingest", jobId: 3, bad: false }, { text: "Threat scan (failed)", jobId: 4, bad: true }]);
});

test("another machine's jobs read without you", () => {
  const [p] = activityPhrases(machine({ jobs: [{ label: "Ingest", kind: "ingest", status: "done", job_id: null }] }));
  assert.equal(p.lead.text, "ran");
  assert.equal(p.parts[0].jobId, undefined);
});

test("spend rounds to dollars from ten up", () => {
  assert.equal(activityPhrases(machine({ spend_usd: 41.3 }))[0].lead.text, "$41 AI");
  assert.equal(activityPhrases(machine({ spend_usd: 3.75 }))[0].lead.text, "$3.75 AI");
  assert.deepEqual(activityPhrases(machine({ spend_usd: 0.001 })), []);
});

test("a machine with nothing to say has no phrases", () => {
  assert.deepEqual(activityPhrases(machine()), []);
});

test("status reads online, offline for how long, or no worker", () => {
  assert.deepEqual(machineStatus(machine(), NOW), [{ text: "● online", tone: "ok" }]);
  assert.deepEqual(machineStatus(machine({ online: false, offline_since: "2026-10-03T09:00:00Z" }), NOW),
    [{ text: "offline 3h", tone: "bad" }]);
  assert.deepEqual(machineStatus(machine({ online: false, has_worker: false }), NOW),
    [{ text: "no worker", tone: "muted" }]);
});

test("paused lanes read beside the status", () => {
  assert.deepEqual(machineStatus(machine({ tripped: ["security", "verify"] }), NOW),
    [{ text: "● online", tone: "ok" }, { text: "security, verify paused", tone: "bad" }]);
});

test("unattributed rows read as an unrecorded machine", () => {
  assert.deepEqual(machineStatus(machine({ host: "unattributed", online: false, has_worker: false }), NOW),
    [{ text: "machine not recorded", tone: "muted" }]);
});
