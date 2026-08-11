import assert from "node:assert/strict";
import { test } from "node:test";
import { CHECK_DEFS } from "../components/explorer/checkDefs.ts";
import {
  ALL_CHECKS_PASS, exploreHref, HOME_CARDS, SAMPLE_QUERY, type HomeCard,
} from "./homeCards.ts";

test("card keys are unique", () => {
  const keys = HOME_CARDS.map((c) => c.key);
  assert.equal(new Set(keys).size, keys.length);
});

test("ALL_CHECKS_PASS requires a pass on every rollup check", () => {
  assert.deepEqual(
    ALL_CHECKS_PASS,
    CHECK_DEFS.map((d) => ({ key: d.key, status: "pass" })),
  );
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
    key: "k", title: "t", blurb: "b", spec: {}, sort: "updated", dir: "asc",
  };
  const unsorted: HomeCard = { key: "k", title: "t", blurb: "b", spec: {} };
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
