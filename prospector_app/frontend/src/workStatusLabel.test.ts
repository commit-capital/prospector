import assert from "node:assert/strict";
import { test } from "node:test";
import { workStatusLabel } from "./workStatusLabel.ts";
import type { WorkStatus } from "./api.ts";

const empty: WorkStatus = {
  active: [],
  queued: { verify: 0, fix: 0 },
  awaiting_review: 0,
  workers: [],
  jobs: { running: 0, labels: [] },
};

const minsAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString();

test("an idle system reads idle", () => {
  assert.equal(workStatusLabel(empty), "idle");
});

test("a single fix run names the PR, its action, and how long it has run", () => {
  const s: WorkStatus = {
    ...empty,
    active: [{ lane: "fix", pr: 908, title: null, action: "rebase", step: "rebasing onto base",
      host: "mac-studio", started_at: minsAgo(5), source: "auto", worker_online: true }],
  };
  assert.equal(workStatusLabel(s), "fix #908 · rebase · 5m");
});

test("a single verify run shows its step instead of an action", () => {
  const s: WorkStatus = {
    ...empty,
    active: [{ lane: "verify", pr: 912, title: null, action: null, step: "sandbox",
      host: "mac-studio", started_at: minsAgo(2), source: null, worker_online: true }],
  };
  assert.equal(workStatusLabel(s), "verify #912 · sandbox · 2m");
});

test("a run started seconds ago omits the elapsed part", () => {
  const s: WorkStatus = {
    ...empty,
    active: [{ lane: "fix", pr: 908, title: null, action: "update", step: "merging base in",
      host: "h", started_at: minsAgo(0), source: null, worker_online: true }],
  };
  assert.equal(workStatusLabel(s), "fix #908 · update");
});

test("a run whose claiming worker stopped beating says so", () => {
  const s: WorkStatus = {
    ...empty,
    active: [{ lane: "fix", pr: 908, title: null, action: "rebase", step: "claimed",
      host: "h", started_at: minsAgo(9), source: null, worker_online: false }],
  };
  assert.equal(workStatusLabel(s), "fix #908 · worker offline?");
});

test("several concurrent runs collapse to a count", () => {
  const a = { lane: "fix", pr: 1, title: null, action: "update", step: null,
    host: "h", started_at: null, source: null, worker_online: true } as const;
  const s: WorkStatus = { ...empty, active: [{ ...a }, { ...a, lane: "verify", pr: 2 }] };
  assert.equal(workStatusLabel(s), "2 running");
});

test("a local job outranks queued work", () => {
  const s: WorkStatus = {
    ...empty,
    queued: { verify: 3, fix: 0 },
    jobs: { running: 1, labels: ["Ingest PRs"] },
  };
  assert.equal(workStatusLabel(s), "1 job");
});

test("queued work with nothing running is a queue count", () => {
  const s: WorkStatus = { ...empty, queued: { verify: 2, fix: 1 } };
  assert.equal(workStatusLabel(s), "3 queued");
});

test("a parked fix awaiting review is surfaced when nothing else is happening", () => {
  const s: WorkStatus = { ...empty, awaiting_review: 2 };
  assert.equal(workStatusLabel(s), "2 to review");
});
