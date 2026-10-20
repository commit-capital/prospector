import assert from "node:assert/strict";
import { test } from "node:test";
import { autoReviewChip } from "./format.ts";

test("the auto-review chip tells an unreviewed resolve from an objected one", () => {
  assert.equal(autoReviewChip({ ok: true, reviewed: 2, objected: false }), "agents cleared");
  assert.equal(autoReviewChip({ ok: false, reviewed: 1, objected: true }), "agents objected");
  assert.equal(autoReviewChip({ ok: false, reviewed: 0, objected: false }), "no agent review");
  assert.equal(autoReviewChip({ ok: false, reviewed: 2, objected: false }), "agents left it for you");
});
