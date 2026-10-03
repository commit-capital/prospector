import assert from "node:assert/strict";
import { test } from "node:test";
import type { SuggestedAction } from "../../api";
import { layoutJobs } from "./jobLayout.ts";

const spec = (kind: string) => ({ kind });
const sug = (kind: string): SuggestedAction => ({ kind, title: kind, reason: `why ${kind}`, last_run: null, count: null, estimate_seconds: null });

test("recommended jobs come first, in suggestion order", () => {
  const { recommended, rest } = layoutJobs([spec("ingest"), spec("threat-scan"), spec("cluster-new")],
    [sug("cluster-new"), sug("ingest")]);
  assert.deepEqual(recommended.map((r) => r.spec.kind), ["cluster-new", "ingest"]);
  assert.equal(recommended[0].suggestion.reason, "why cluster-new");
  assert.deepEqual(rest.map((s) => s.kind), ["threat-scan"]);
});

test("a suggestion for an unknown job, or repeated, counts once or not at all", () => {
  const { recommended } = layoutJobs([spec("ingest")], [sug("nope"), sug("ingest"), sug("ingest")]);
  assert.deepEqual(recommended.map((r) => r.spec.kind), ["ingest"]);
});

test("no suggestions leaves every job a tile", () => {
  const { recommended, rest } = layoutJobs([spec("ingest"), spec("threat-scan")], []);
  assert.deepEqual(recommended, []);
  assert.equal(rest.length, 2);
});
