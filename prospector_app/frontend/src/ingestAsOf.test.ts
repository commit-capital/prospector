import assert from "node:assert/strict";
import { test } from "node:test";
import { ingestedCount, knownUntil, localDay, showAxisLabel } from "./ingestAsOf.ts";

// A fixed local-noon anchor keeps every derived stamp inside one local
// calendar day whatever the zone, so the tests are deterministic.
const ANCHOR = new Date(2026, 5, 24, 12, 0, 0);

function axisEndingAtAnchor(n: number): string[] {
  const out: string[] = [];
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(ANCHOR);
    d.setDate(ANCHOR.getDate() - i);
    out.push(localDay(d));
  }
  return out;
}

function isoDaysBeforeAnchor(n: number): string {
  const d = new Date(ANCHOR);
  d.setDate(ANCHOR.getDate() - n);
  return d.toISOString();
}

test("knownUntil with no stamp covers every day", () => {
  const days = axisEndingAtAnchor(30);
  assert.equal(knownUntil(days, null), 29);
  assert.equal(knownUntil(days, undefined), 29);
  assert.equal(knownUntil(days, "not-a-date"), 29);
});

test("knownUntil stops at the ingest stamp's local day", () => {
  const days = axisEndingAtAnchor(30);
  // 30-day axis ending at the anchor; a stamp 19 days back covers index 10.
  assert.equal(knownUntil(days, isoDaysBeforeAnchor(19)), 10);
  assert.equal(knownUntil(days, isoDaysBeforeAnchor(0)), 29);
});

test("knownUntil is -1 when the stamp predates the window", () => {
  const days = axisEndingAtAnchor(7);
  assert.equal(knownUntil(days, isoDaysBeforeAnchor(19)), -1);
});

test("ingestedCount keeps the number while ingest reaches into the window", () => {
  const days = axisEndingAtAnchor(30);
  assert.equal(ingestedCount(5, isoDaysBeforeAnchor(19), days[0]), 5);
  assert.equal(ingestedCount(5, null, days[0]), 5);
  assert.equal(ingestedCount(5, isoDaysBeforeAnchor(19), undefined), 5);
});

test("ingestedCount reads unknown, not zero, when the window is past the last ingest", () => {
  // Ingest 19 days stale: a this-week count is unknown, never a 0.
  const weekStart = axisEndingAtAnchor(7)[0];
  assert.equal(ingestedCount(0, isoDaysBeforeAnchor(19), weekStart), "—");
});

test("axis labels keep the cadence and the final day", () => {
  // 30 days, step 4: 28 would crowd 29, so it is skipped.
  assert.equal(showAxisLabel(0, 30, 4), true);
  assert.equal(showAxisLabel(4, 30, 4), true);
  assert.equal(showAxisLabel(24, 30, 4), true);
  assert.equal(showAxisLabel(28, 30, 4), false);
  assert.equal(showAxisLabel(29, 30, 4), true);
  // Step 1 (short axes) keeps every label.
  for (let i = 0; i < 7; i++) assert.equal(showAxisLabel(i, 7, 1), true);
});
