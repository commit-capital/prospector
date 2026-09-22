import assert from "node:assert/strict";
import { test } from "node:test";
import { factLine } from "./factLine.ts";
import type { FactFreshness } from "./api.ts";

const daysAgo = (d: number) => new Date(Date.now() - d * 24 * 3600_000).toISOString();

const fact = (section: string, current: boolean, checkedDaysAgo: number): FactFreshness =>
  ({ section, current, checked_at: daysAgo(checkedDaysAgo), against_head_sha: "abc1234def" } as FactFreshness);

test("all-current facts read as one calm line", () => {
  const facts = Array.from({ length: 8 }, (_, i) => fact(`f${i}`, true, 1));
  assert.equal(factLine(facts), "All 8 facts current");
});

test("staleness names the most out-of-date stale fact with its age", () => {
  const facts = [
    fact("signals", true, 1),
    fact("security", false, 8),
    ...Array.from({ length: 6 }, (_, i) => fact(`f${i}`, true, 2)),
  ];
  assert.equal(factLine(facts), "1 of 8 facts stale · Security review 8d ago");
});

test("two stale facts count both but name the older one", () => {
  const facts = [fact("security", false, 8), fact("analysis", false, 3), fact("drift", true, 1)];
  assert.equal(factLine(facts), "2 of 3 facts stale · Security review 8d ago");
});

test("a moved head outranks staleness and names the analyzed head", () => {
  const facts = [fact("security", false, 8)];
  assert.equal(factLine(facts, "abc1234def5678", "fff999888"),
    "Author pushed since — these facts describe abc1234");
});
