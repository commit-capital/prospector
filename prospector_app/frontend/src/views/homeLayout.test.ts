import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// The Home sample table gives every cell but the title a fixed width and the
// title whatever is left, so these tests hold the stylesheet to the pairing
// that keeps titles readable: the card's sample area declares itself a size
// container, and @container rules drop the issues cell, then the pain cell,
// before the fixed widths squeeze the title below a readable share.
const css = readFileSync(new URL("../styles.css", import.meta.url), "utf8");

// The least width the title cell may be left in any card the thresholds allow.
const MIN_TITLE_PX = 150;

function px(pattern: RegExp): number {
  const m = css.match(pattern);
  assert.ok(m, `no match for ${pattern}`);
  return Number(m[1]);
}

const prWidth = px(/\.home-sample-pr\s*\{[^}]*width:\s*(\d+)px/);
const painWidth = px(/\.home-sample-pain\s*\{[^}]*width:\s*(\d+)px/);
const issuesWidth = px(/\.home-sample-issues\s*\{[^}]*width:\s*(\d+)px/);
const cellPad = px(/\.home-sample-table td \+ td\s*\{[^}]*padding-left:\s*(\d+)px/);
const issuesDrop = px(/@container \(width < (\d+)px\)\s*\{\s*\.home-sample-issues\s*\{\s*display:\s*none/);
const painDrop = px(/@container \(width < (\d+)px\)\s*\{\s*\.home-sample-pain\s*\{\s*display:\s*none/);

test("the sample area is the size container the @container rules measure", () => {
  assert.match(css, /\.home-card-side\s*\{[^}]*container-type:\s*inline-size/);
});

test("the issues cell drops before the title falls below its readable share", () => {
  assert.ok(issuesDrop >= prWidth + painWidth + issuesWidth + 3 * cellPad + MIN_TITLE_PX);
});

test("the pain cell drops before the title falls below its readable share", () => {
  assert.ok(painDrop >= prWidth + painWidth + 2 * cellPad + MIN_TITLE_PX);
});

test("the issues cell drops first", () => {
  assert.ok(painDrop < issuesDrop);
});
