import assert from "node:assert/strict";
import { test } from "node:test";
import type { TripAlert } from "./api.ts";
import { tripHeadline, tripReason } from "./tripBanner.ts";

function alert(over: Partial<TripAlert>): TripAlert {
  return { host: "studio", lane: "verify", tripped: true, at: "2026-10-21T02:46:33+00:00",
           kind: "pin-stale", reason: "pin 5 days old", remedy: null, trips_24h: 1, ...over };
}

test("a tripped lane reads as stopped, naming machine and kind", () => {
  assert.equal(tripHeadline(alert({})), "verify lane stopped on studio · pin-stale");
});

test("a tripped lane that tripped before today says how often", () => {
  assert.equal(tripHeadline(alert({ trips_24h: 3 })),
               "verify lane stopped on studio · pin-stale · 3× in 24h");
});

test("an open lane that keeps retripping reads as a count", () => {
  assert.equal(tripHeadline(alert({ lane: "design", tripped: false, kind: "coverage", trips_24h: 4 })),
               "design lane on studio tripped 4× in 24h · coverage");
});

test("a long reason is cut, a short one kept", () => {
  assert.equal(tripReason(alert({})), "pin 5 days old");
  const cut = tripReason(alert({ reason: "x".repeat(300) }));
  assert.ok(cut.endsWith("…") && cut.length === 161);
});
