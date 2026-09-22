import assert from "node:assert/strict";
import { test } from "node:test";
import { ALL_CHECKS_PASS, CHECK_DEFS } from "../components/explorer/checkDefs.ts";
import { LANES } from "../components/explorer/lanes.ts";
import {
  breakdownHref, exploreHref, HOME_BREAKDOWN_ENTRIES, HOME_CARDS, HOME_COUNT_SPECS,
  HOME_ISSUE_CARDS, ISSUE_ANALYZE_BATCH, issuesHref,
  painLabel, SAMPLE_LIMIT, SAMPLE_QUERY,
  SECURITY_ADVISORIES_HREF, SECURITY_ADVISORY_QUERY, SECURITY_ALERT_QUERY,
  SECURITY_ALERTS_HREF, SECURITY_CARD,
  type HomeCard,
} from "./homeCards.ts";

test("card keys are unique", () => {
  const keys = HOME_CARDS.map((c) => c.key);
  assert.equal(new Set(keys).size, keys.length);
});

test("cards run your move, then in motion, then handed back", () => {
  assert.deepEqual(
    HOME_CARDS.map((c) => c.key),
    ["ready", "approve", "queued", "hunt", "waiting", "author", "your-call"],
  );
  assert.deepEqual(
    HOME_CARDS.map((c) => c.column),
    ["act", "act", "auto", "auto", "auto", "handed", "handed"],
  );
});

test("every card filters on the automation standing alone", () => {
  for (const card of HOME_CARDS) {
    const keys = Object.keys(card.spec);
    assert.ok(keys.every((k) => k.startsWith("automation_")), card.key);
    assert.equal(keys.length, 1, card.key);
  }
});

test("the handed-back cards carry a reason breakdown inside their own spec", () => {
  const handed = HOME_CARDS.filter((c) => c.column === "handed");
  assert.equal(handed.length, 2);
  for (const card of handed) {
    assert.ok((card.breakdown ?? []).length >= 4, card.key);
    for (const entry of card.breakdown ?? []) {
      assert.deepEqual(Object.keys(entry.spec), ["automation_bucket"], `${card.key}/${entry.label}`);
    }
  }
  for (const card of HOME_CARDS.filter((c) => c.column !== "handed")) {
    assert.equal(card.breakdown, undefined, card.key);
  }
});

test("the counts request lists card specs first, then every breakdown entry in order", () => {
  assert.deepEqual(HOME_COUNT_SPECS.slice(0, HOME_CARDS.length), HOME_CARDS.map((c) => c.spec));
  assert.deepEqual(HOME_COUNT_SPECS.slice(HOME_CARDS.length), HOME_BREAKDOWN_ENTRIES.map((e) => e.entry.spec));
  assert.deepEqual(HOME_BREAKDOWN_ENTRIES.map((e) => e.cardKey),
    HOME_CARDS.flatMap((c) => (c.breakdown ?? []).map(() => c.key)));
  assert.ok(HOME_COUNT_SPECS.length <= 20, "the counts route accepts at most 20 specs");
});

test("a breakdown link keeps the card's sort and takes the entry's spec", () => {
  const card = HOME_CARDS.find((c) => c.key === "author")!;
  const entry = card.breakdown![0];
  const params = new URLSearchParams(breakdownHref(card, entry).slice("/explore?".length));
  assert.deepEqual(JSON.parse(params.get("spec")!), entry.spec);
});

test("ALL_CHECKS_PASS requires a pass on every rollup check", () => {
  assert.deepEqual(
    ALL_CHECKS_PASS,
    CHECK_DEFS.map((d) => ({ key: d.key, status: "pass" })),
  );
});

test("the ready card is the merge-ready standing, oldest first", () => {
  const ready = HOME_CARDS.find((c) => c.key === "ready")!;
  assert.deepEqual(ready.spec, { automation_bucket: "merge-ready" });
  assert.equal(ready.sort, "updated");
  assert.equal(ready.dir, "asc");
});

test("every lane spec uses only fields the filter UI can represent", () => {
  // The chip bar / column popouts cover these spec fields; a lane must never
  // carry a field the operator can't see or edit after clicking it.
  const representable = new Set([
    "q", "author", "cluster", "cluster_none", "safety", "drift", "disposition",
    "ci", "checks", "threat", "conflicts", "has_tests", "draft", "state",
    "trusted_author", "clean", "greptile", "greptile_stale", "greptile_severity", "reviewer_status",
    "age_days", "risk_tier", "responses", "loc", "files", "pain", "author_rate",
    "artifact_dominated", "paths", "numbers", "merge_ok", "has_summary", "has_issues",
    "automation_column", "automation_bucket", "automation_owner",
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
    key: "k", title: "t", blurb: "b", column: "act", spec: {}, sort: "updated", dir: "asc",
  };
  const unsorted: HomeCard = { key: "k", title: "t", blurb: "b", column: "act", spec: {} };
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

test("cards sample two PRs each so every card fits above the fold", () => {
  assert.equal(SAMPLE_LIMIT, 2);
});

test("issue card keys are unique and disjoint from PR card keys", () => {
  const keys = [...HOME_CARDS.map((c) => c.key), ...HOME_ISSUE_CARDS.map((c) => c.key)];
  assert.equal(new Set(keys).size, keys.length);
});

test("the issue cards cover the close-fixed picks and the unanalyzed backlog", () => {
  assert.deepEqual(HOME_ISSUE_CARDS.map((c) => c.key), ["issues-close-fixed", "issues-unanalyzed"]);
  assert.deepEqual(HOME_ISSUE_CARDS.map((c) => c.disposition), ["close-fixed", "none"]);
});

test("only the unanalyzed card runs a job, and it runs issue-analyze", () => {
  const actions = Object.fromEntries(HOME_ISSUE_CARDS.map((c) => [c.key, c.action ?? null]));
  assert.equal(actions["issues-close-fixed"], null);
  assert.deepEqual(actions["issues-unanalyzed"],
    { kind: "issue-analyze", label: "Analyze", batch: ISSUE_ANALYZE_BATCH });
});

test("issuesHref carries the card's disposition filter", () => {
  for (const card of HOME_ISSUE_CARDS) {
    const href = issuesHref(card);
    assert.ok(href.startsWith("/issues?"));
    const params = new URLSearchParams(href.slice("/issues?".length));
    assert.equal(params.get("disposition"), card.disposition);
  }
});

test("no card runs a per-row job — the workers pick these up themselves", () => {
  for (const card of HOME_CARDS) {
    assert.equal(card.rowAction, undefined, card.key);
  }
});

test("the security card's key is disjoint from every other card's", () => {
  const keys = [...HOME_CARDS.map((c) => c.key), ...HOME_ISSUE_CARDS.map((c) => c.key)];
  assert.ok(!keys.includes(SECURITY_CARD.key));
});

test("the security card counts open critical/high not-fixed advisories", () => {
  assert.deepEqual(SECURITY_ADVISORY_QUERY.state, ["triage", "draft"]);
  assert.deepEqual(SECURITY_ADVISORY_QUERY.severity, ["critical", "high"]);
  assert.equal(SECURITY_ADVISORY_QUERY.verdict, "not-fixed");
});

test("the security card counts every open secret-scanning alert", () => {
  assert.equal(SECURITY_ALERT_QUERY.source, "secret-scanning");
  assert.equal(SECURITY_ALERT_QUERY.state, "open");
});

test("both security samples fetch the row budget, most severe first", () => {
  for (const q of [SECURITY_ADVISORY_QUERY, SECURITY_ALERT_QUERY]) {
    assert.equal(q.sort, "severity");
    assert.equal(q.limit, SAMPLE_LIMIT);
  }
});

test("the security links open the Security views' two sections", () => {
  assert.equal(SECURITY_CARD.href, "/alerts");
  assert.ok(SECURITY_ADVISORIES_HREF.startsWith("/alerts?"));
  assert.ok(SECURITY_ALERTS_HREF.startsWith("/alerts?"));
  const advisoryParams = new URLSearchParams(SECURITY_ADVISORIES_HREF.slice("/alerts?".length));
  const alertParams = new URLSearchParams(SECURITY_ALERTS_HREF.slice("/alerts?".length));
  assert.equal(advisoryParams.get("security"), "advisories");
  assert.equal(alertParams.get("security"), "alerts");
});

test("painLabel formats a score to two decimals and hides missing ones", () => {
  assert.equal(painLabel(3.2), "🔥 3.20");
  assert.equal(painLabel(12.345), "🔥 12.35");
  assert.equal(painLabel(0), "");
  assert.equal(painLabel(null), "");
  assert.equal(painLabel(undefined), "");
});
