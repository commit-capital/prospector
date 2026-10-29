import assert from "node:assert/strict";
import { test } from "node:test";
import { MOCKUP_CSP, mockupSrcdoc } from "./mockupSrcdoc.ts";

test("csp meta is the first thing in head", () => {
  const out = mockupSrcdoc("<html><head><style>a{}</style></head><body></body></html>");
  assert.ok(out.indexOf("Content-Security-Policy") < out.indexOf("<style>"));
});

test("adds head when missing", () => {
  assert.match(mockupSrcdoc("<html><body>x</body></html>"), /<head><meta http-equiv="Content-Security-Policy"/);
});

test("csp forbids every fetch", () => {
  assert.match(MOCKUP_CSP, /default-src 'none'/);
  assert.doesNotMatch(MOCKUP_CSP, /http|\*/);
});
