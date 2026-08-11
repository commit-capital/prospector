import assert from "node:assert/strict";
import { test } from "node:test";
import type { PRRow, QueryResult } from "../../api";
import { reuseRows } from "./rowReuse.ts";

function row(n: number, extra: Partial<PRRow> = {}): PRRow {
  return {
    number: n, title: `PR ${n}`, author: "someone", draft: false,
    clusters: [], disposition: null, safety: null, safety_findings: 0,
    drift_state: null, issues: [],
    ...extra,
  };
}

function result(items: PRRow[]): QueryResult {
  return { items, total: items.length, offset: 0, limit: 50,
           match_ids: items.map((r) => r.number) };
}

test("first result passes through untouched", () => {
  const next = result([row(1), row(2)]);
  assert.equal(reuseRows(null, next), next);
});

test("a content-identical row keeps its previous object identity", () => {
  const prev = result([row(1), row(2)]);
  const next = result([row(1), row(2)]);
  const merged = reuseRows(prev, next);
  assert.equal(merged.items[0], prev.items[0]);
  assert.equal(merged.items[1], prev.items[1]);
});

test("a changed row takes the fresh object", () => {
  const prev = result([row(1), row(2)]);
  const next = result([row(1), row(2, { title: "retitled" })]);
  const merged = reuseRows(prev, next);
  assert.equal(merged.items[0], prev.items[0]);
  assert.equal(merged.items[1], next.items[1]);
  assert.equal(merged.items[1].title, "retitled");
});

test("rows are matched by PR number, not list position", () => {
  const prev = result([row(1), row(2), row(3)]);
  const next = result([row(3), row(1)]);
  const merged = reuseRows(prev, next);
  assert.equal(merged.items[0], prev.items[2]);
  assert.equal(merged.items[1], prev.items[0]);
});

const SIGNALS = {
  greptile: 4, greptile_stale: false, greptile_severity: null, ci: "passing",
  conflicts: false, additions: 10, deletions: 2, changed_files: 3,
};

test("a nested content change is a changed row", () => {
  const prev = result([row(1, { signals: SIGNALS })]);
  const next = result([row(1, { signals: { ...SIGNALS, greptile: 5 } })]);
  const merged = reuseRows(prev, next);
  assert.equal(merged.items[0], next.items[0]);
});

test("result-level fields always come from the fresh result", () => {
  const prev = result([row(1)]);
  const next = { ...result([row(1)]), total: 99, match_ids: [1, 2, 3] };
  const merged = reuseRows(prev, next);
  assert.equal(merged.total, 99);
  assert.deepEqual(merged.match_ids, [1, 2, 3]);
});
