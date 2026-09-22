import assert from "node:assert/strict";
import { test } from "node:test";
import { ALL_CHECKS_PASS, CHECK_DEFS } from "../components/explorer/checkDefs.ts";
import { LANES } from "../components/explorer/lanes.ts";
import type { AdvisoryRow, AdvisorySeverity, AlertRow, AlertSeverity } from "../api.ts";
import {
  advisoryHref, breakdownHref, exploreHref, HOME_BREAKDOWN_ENTRIES, HOME_CARDS, HOME_COUNT_SPECS,
  HOME_ISSUE_CARDS, HOME_SECURITY_CARD, ISSUE_ANALYZE_BATCH, issuesHref,
  painLabel, SAMPLE_LIMIT, SAMPLE_QUERY,
  SECURITY_ADVISORY_QUERY, SECURITY_ALERT_QUERY, secretAlertHref, securityHref, securitySamples,
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

test("issue and security card keys are unique and disjoint from PR card keys", () => {
  const keys = [
    ...HOME_CARDS.map((c) => c.key), ...HOME_ISSUE_CARDS.map((c) => c.key),
    HOME_SECURITY_CARD.key,
  ];
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

test("the security card's advisory query asks for open critical/high not-fixed reports", () => {
  assert.deepEqual(SECURITY_ADVISORY_QUERY.state, ["triage", "draft"]);
  assert.deepEqual(SECURITY_ADVISORY_QUERY.severity, ["critical", "high"]);
  assert.equal(SECURITY_ADVISORY_QUERY.verdict, "not-fixed");
  assert.equal(SECURITY_ADVISORY_QUERY.sort, "severity");
  assert.equal(SECURITY_ADVISORY_QUERY.direction, "desc");
  assert.equal(SECURITY_ADVISORY_QUERY.limit, SAMPLE_LIMIT);
});

test("the security card's alert query asks for every open secret-scanning alert", () => {
  assert.equal(SECURITY_ALERT_QUERY.source, "secret-scanning");
  assert.equal(SECURITY_ALERT_QUERY.state, "open");
  assert.equal(SECURITY_ALERT_QUERY.sort, "severity");
  assert.equal(SECURITY_ALERT_QUERY.direction, "desc");
  assert.equal(SECURITY_ALERT_QUERY.limit, SAMPLE_LIMIT);
});

test("security hrefs open the 🛡️ Alerts tab's sections and detail panes", () => {
  assert.equal(securityHref("advisories"), "/alerts?security=advisories");
  assert.equal(securityHref("alerts"), "/alerts?security=alerts");
  assert.equal(advisoryHref("GHSA-1111-2222-3333"),
    "/alerts?security=advisories&advisory=GHSA-1111-2222-3333");
  assert.equal(secretAlertHref(7), "/alerts?security=alerts&alert_source=secret-scanning&alert=7");
});

test("security samples merge both families, most severe then oldest, capped at the row budget", () => {
  const advisory = (ghsa: string, severity: AdvisorySeverity, created: string): AdvisoryRow => {
    const row: Partial<AdvisoryRow> = { ghsa_id: ghsa, severity, summary: `s-${ghsa}`, created_at: created };
    return row as AdvisoryRow;
  };
  const alert = (n: number, severity: AlertSeverity, created: string): AlertRow => {
    const row: Partial<AlertRow> = { number: n, severity, title: `a-${n}`, secret_type: "pat", created_at: created };
    return row as AlertRow;
  };
  const samples = securitySamples(
    [advisory("GHSA-a", "high", "2026-01-05"), advisory("GHSA-b", "critical", "2026-03-01")],
    [alert(1, "critical", "2026-02-01")],
  );
  assert.deepEqual(samples.map((s) => s.key), ["alert-1", "advisory-GHSA-b"].slice(0, SAMPLE_LIMIT));
  assert.equal(samples.length, Math.min(3, SAMPLE_LIMIT));
  assert.equal(samples[0].href, secretAlertHref(1));
  assert.equal(samples[0].ref, "#1");
  assert.equal(samples[0].text, "a-1");
});

test("painLabel formats a score to two decimals and hides missing ones", () => {
  assert.equal(painLabel(3.2), "🔥 3.20");
  assert.equal(painLabel(12.345), "🔥 12.35");
  assert.equal(painLabel(0), "");
  assert.equal(painLabel(null), "");
  assert.equal(painLabel(undefined), "");
});
