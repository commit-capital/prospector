import assert from "node:assert/strict";
import { test } from "node:test";
import type { CheckItem, ChecksRollup } from "../api";
import { liveConflicts, withLiveConflicts } from "./liveChecks.ts";

function rollup(...checks: CheckItem[]): ChecksRollup {
  return { checks, passed: checks.filter((c) => c.status === "pass").length, total: checks.length };
}

const CI: CheckItem = { key: "ci", name: "CI", status: "pass", detail: "passing" };
const MERGEABLE: CheckItem = { key: "mergeable", name: "No merge conflicts", status: "pass", detail: "clean" };
const CONFLICTS = [{ kind: "conflicts", message: "now has merge conflicts" }];

test("a live conflict fails the stored clean merge row", () => {
  const out = withLiveConflicts(rollup(CI, MERGEABLE), CONFLICTS)!;
  const row = out.checks.find((c) => c.key === "mergeable")!;
  assert.equal(row.status, "fail");
  assert.match(row.detail, /has conflicts/);
  assert.equal(out.passed, 1);
  assert.equal(out.total, 2);
});

test("other divergences leave the rollup as stored", () => {
  const c = rollup(CI, MERGEABLE);
  assert.equal(withLiveConflicts(c, [{ kind: "head", message: "new commits" }]), c);
  assert.equal(withLiveConflicts(c, undefined), c);
});

test("a not-yet-checked merge row counts once the live check fails it", () => {
  const out = withLiveConflicts(rollup(CI, { ...MERGEABLE, status: "na" }), CONFLICTS)!;
  assert.equal(out.total, 2);
  assert.equal(out.passed, 1);
});

test("liveConflicts reads only the conflicts kind", () => {
  assert.equal(liveConflicts(CONFLICTS), true);
  assert.equal(liveConflicts([{ kind: "ci", message: "CI is now failing" }]), false);
  assert.equal(liveConflicts(undefined), false);
});
