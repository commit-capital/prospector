import assert from "node:assert/strict";
import { test } from "node:test";
import { HttpError, isNotFound } from "./httpError.ts";

test("HttpError keeps the url → status message", () => {
  assert.equal(new HttpError("/api/prs/1", 500).message, "/api/prs/1 → 500");
});

test("only a 404 HttpError reads as not found", () => {
  assert.equal(isNotFound(new HttpError("/api/prs/1", 404)), true);
  assert.equal(isNotFound(new HttpError("/api/prs/1", 500)), false);
  assert.equal(isNotFound(new Error("/api/prs/1 → 404")), false);
  assert.equal(isNotFound(undefined), false);
});
