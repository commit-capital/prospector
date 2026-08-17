import assert from "node:assert/strict";
import { test } from "node:test";
import { ALL_CHECKS_PASS, CHECK_DEFS } from "../components/explorer/checkDefs.ts";
import { LANES, MERGE_READY_SPEC } from "../components/explorer/lanes.ts";
import {
  exploreHref, HOME_CARDS, painLabel, SAMPLE_LIMIT, SAMPLE_QUERY,
  type HomeCard,
} from "./homeCards.ts";

test("card keys are unique", () => {
  const keys = HOME_CARDS.map((c) => c.key);
  assert.equal(new Set(keys).size, keys.length);
});

test("cards run merge track first, then the changes column, then the backstop", () => {
  assert.deepEqual(
    HOME_CARDS.map((c) => c.key),
    ["ready", "verify-pending", "security-pending", "base-update", "changes", "nitpicks", "needs-human"],
  );
  assert.deepEqual(
    HOME_CARDS.map((c) => c.column),
    ["merge", "merge", "merge", "merge", "changes", "changes", "backstop"],
  );
});

test("merge-column cards keep only the pipeline's merge picks", () => {
  for (const card of HOME_CARDS.filter((c) => c.column === "merge")) {
    assert.equal(card.spec.disposition, "merge", card.key);
  }
});

test("changes-column cards keep only the request-changes picks", () => {
  for (const card of HOME_CARDS.filter((c) => c.column === "changes")) {
    assert.equal(card.spec.disposition, "request-changes", card.key);
  }
});

test("ALL_CHECKS_PASS requires a pass on every rollup check", () => {
  assert.deepEqual(
    ALL_CHECKS_PASS,
    CHECK_DEFS.map((d) => ({ key: d.key, status: "pass" })),
  );
});

test("the ready card is the Merge-ready lane spec narrowed to merge picks", () => {
  const ready = HOME_CARDS.find((c) => c.key === "ready")!;
  const lane = LANES.find((l) => l.key === "merge-ready")!;
  assert.deepEqual(ready.spec, { ...MERGE_READY_SPEC, disposition: "merge" });
  assert.equal(lane.spec, MERGE_READY_SPEC);
});

test("every lane spec uses only fields the filter UI can represent", () => {
  // The chip bar / column popouts cover these spec fields; a lane must never
  // carry a field the operator can't see or edit after clicking it.
  const representable = new Set([
    "q", "author", "cluster", "cluster_none", "safety", "drift", "disposition",
    "ci", "checks", "threat", "conflicts", "has_tests", "draft", "state",
    "trusted_author", "clean", "greptile", "greptile_stale", "greptile_severity",
    "age_days", "risk_tier", "responses", "loc", "files", "pain", "author_rate",
    "artifact_dominated", "paths", "numbers", "merge_ok", "has_summary", "has_issues",
  ]);
  for (const lane of LANES) {
    for (const field of Object.keys(lane.spec)) {
      assert.ok(representable.has(field), `${lane.key} uses unrepresentable field ${field}`);
    }
  }
});

test("exploreHref round-trips the spec through the URL", () => {
  for (const card of HOME_CARDS) {
    const href = exploreHref(card);
    assert.ok(href.startsWith("/explore?"));
    const params = new URLSearchParams(href.slice("/explore?".length));
    assert.deepEqual(JSON.parse(params.get("spec")!), card.spec);
  }
});

test("exploreHref carries sort and dir only when the card sets them", () => {
  const sorted: HomeCard = {
    key: "k", title: "t", blurb: "b", column: "merge", spec: {}, sort: "updated", dir: "asc",
  };
  const unsorted: HomeCard = { key: "k", title: "t", blurb: "b", column: "merge", spec: {} };
  const sortedParams = new URLSearchParams(exploreHref(sorted).slice("/explore?".length));
  assert.equal(sortedParams.get("sort"), "updated");
  assert.equal(sortedParams.get("dir"), "asc");
  const unsortedParams = new URLSearchParams(exploreHref(unsorted).slice("/explore?".length));
  assert.equal(unsortedParams.get("sort"), null);
  assert.equal(unsortedParams.get("dir"), null);
});

test("card samples ask for the highest-pain PRs first", () => {
  assert.equal(SAMPLE_QUERY.sort, "pain");
  assert.equal(SAMPLE_QUERY.direction, "desc");
  assert.ok(SAMPLE_QUERY.limit > 0);
});

test("the sample query fetches exactly the table's row budget", () => {
  assert.equal(SAMPLE_QUERY.limit, SAMPLE_LIMIT);
});

test("painLabel formats a score to two decimals and hides missing ones", () => {
  assert.equal(painLabel(3.2), "🔥 3.20");
  assert.equal(painLabel(12.345), "🔥 12.35");
  assert.equal(painLabel(0), "");
  assert.equal(painLabel(null), "");
  assert.equal(painLabel(undefined), "");
});
