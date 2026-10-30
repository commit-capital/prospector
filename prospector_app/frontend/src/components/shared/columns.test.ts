import assert from "node:assert/strict";
import { test } from "node:test";
import { readWrap } from "./columns.ts";

test("long text wraps only after the operator turns it on", () => {
  assert.equal(readWrap(null), false);
  assert.equal(readWrap("0"), false);
  assert.equal(readWrap("1"), true);
});
