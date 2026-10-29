import assert from "node:assert/strict";
import { test } from "node:test";
import { MOCKUP_CSP, mockupSrcdoc } from "./mockupSrcdoc.ts";

const PREFIX = `<!doctype html><meta http-equiv="Content-Security-Policy" content="${MOCKUP_CSP}">`;

test("output starts with the doctype and the csp meta", () => {
  assert.ok(mockupSrcdoc("<html><head><style>a{}</style></head><body></body></html>").startsWith(PREFIX));
});

test("csp meta precedes every other element", () => {
  const out = mockupSrcdoc("<html><head><style>a{}</style></head><body></body></html>");
  assert.ok(out.indexOf("Content-Security-Policy") < out.indexOf("<html"));
  assert.ok(out.indexOf("Content-Security-Policy") < out.indexOf("<style>"));
});

test("a head inside an attribute value does not move the meta", () => {
  const out = mockupSrcdoc(`<div title="<head>"><img src="https://example.com/x.png"></div>`);
  assert.ok(out.startsWith(PREFIX));
  assert.equal(out.split("Content-Security-Policy").length, 2);
});

test("a head after body does not move the meta", () => {
  const out = mockupSrcdoc("<body><img src=x></body><head></head>");
  assert.ok(out.startsWith(PREFIX));
  assert.equal(out.split("Content-Security-Policy").length, 2);
});

test("a head with attributes keeps the meta first", () => {
  const out = mockupSrcdoc("<HEAD lang=x><style>a{}</style></HEAD>");
  assert.ok(out.startsWith(PREFIX));
  assert.ok(out.indexOf("Content-Security-Policy") < out.indexOf("<HEAD"));
});

test("input without a doctype gets the prefix", () => {
  assert.equal(mockupSrcdoc("<p>x</p>"), `${PREFIX}<p>x</p>`);
});

test("one leading doctype is stripped, case-insensitively and past whitespace", () => {
  assert.equal(mockupSrcdoc("﻿ \n<!DOCTYPE HTML PUBLIC 'x'><p>x</p>"), `${PREFIX}<p>x</p>`);
});

test("csp forbids every fetch", () => {
  assert.match(MOCKUP_CSP, /default-src 'none'/);
  assert.doesNotMatch(MOCKUP_CSP, /http|\*/);
});
