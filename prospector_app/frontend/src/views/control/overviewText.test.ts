import assert from "node:assert/strict";
import { test } from "node:test";
import { cellLabel, healthLane, nextText, statusLine } from "./overviewText.ts";
import type { ControlOverview, OverviewLane } from "../../api.ts";

const lane = (over: Partial<OverviewLane>): OverviewLane => ({
  lane: "verify", label: "Verify", cells: [], now: [], queue: [], picks: [], parked: 0, pool: null,
  ended_24h: 0, faults_24h: 0, ...over });

test("cell labels read as words", () => {
  assert.equal(cellLabel("ai-paused"), "AI paused");
  assert.equal(cellLabel("stood-down"), "stood down");
  assert.equal(cellLabel("on"), "on");
});

test("next prefers the queue head, then the pool, then nothing", () => {
  const q = { pr: 8, issue: null, title: "b", status: "queued", source: "operator", queued_at: null, priority: null };
  assert.equal(nextText(lane({ queue: [q, q] }), null), "#8 · 2 queued");
  assert.equal(nextText(lane({ pool: 4 }), null), "4 in hunter pool");
  assert.equal(nextText(lane({ pool: 0 }), null), "nothing waiting");
  assert.equal(nextText(lane({ cells: [{ host: "a", state: "ai-paused" }] }), "2026-10-14T23:00:00Z"),
    "after AI resumes");
});

test("status line names working, off and offline machines and the AI hold", () => {
  const o = {
    machines: [
      { host: "studio", local: true, state: "working", last_beat: null, lanes_on: 2, stood_down_note: null },
      { host: "lap", local: false, state: "off", last_beat: null, lanes_on: 0, stood_down_note: null },
    ],
    ai_paused: { reason: "weekly limit", retry_at: null },
  } as unknown as ControlOverview;
  assert.equal(statusLine(o), "studio working on 2 lanes · lap off · background AI paused: weekly limit");
});

test("a lane off everywhere shows no pool", () => {
  assert.equal(nextText(lane({ pool: 2577, cells: [{ host: "a", state: "off" }, { host: "b", state: "stood-down" }] }), null), "—");
  assert.equal(nextText(lane({ pool: 3, cells: [{ host: "a", state: "on" }] }), null), "3 in hunter pool");
});

test("a paused lane reads paused and shows no pool", () => {
  assert.equal(cellLabel("paused"), "paused");
  assert.equal(nextText(lane({ pool: 9, cells: [{ host: "a", state: "paused" }] }), null), "—");
});

test("health lane names map the overview's lanes", () => {
  assert.equal(healthLane("autofix"), "fix");
  assert.equal(healthLane("issue-fix"), "issue-fix");
  assert.equal(healthLane("verify"), "verify");
});
