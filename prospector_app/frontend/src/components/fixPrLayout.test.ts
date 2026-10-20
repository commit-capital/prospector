import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Issue Summary, "PRs that may fix it": a PR's fact chips sit in the same
// column as its title, and the analysis below keeps its distance.
const css = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
const tsx = readFileSync(new URL("./issues/IssueSummary.tsx", import.meta.url), "utf8");

test("a fix PR row puts the ref in one column and the title with its facts in the next", () => {
  assert.match(css, /\.fix-pr-row\s*\{[^}]*display:\s*grid;[^}]*grid-template-columns:\s*auto 1fr/);
  assert.match(tsx, /<div className="fix-pr-body">\s*<span className="fix-pr-title"[^]*?<div className="fix-pr-facts">[^]*?<\/div>\s*<\/div>/);
});

test("the analysis is set apart from the PRs above it", () => {
  assert.match(css, /\.issue-analysis\s*\{[^}]*margin-top:\s*\d+px/);
});
