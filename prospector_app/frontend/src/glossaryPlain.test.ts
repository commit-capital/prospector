import assert from "node:assert/strict";
import { test } from "node:test";
import { TERMS, clusterStateEntry, dispositionEntry, safetyEntry, type GlossaryEntry } from "./glossary.ts";
import type { ClusterState, Disposition } from "./api.ts";

const DISPOSITIONS: Disposition[] = ["merge", "request-changes", "close-dup", "close-fixed", "close-stale", "close-oversized", "needs-human"];
const CLUSTER_STATES: ClusterState[] = ["needs-analysis", "awaiting-authors", "needs-first-party-work", "blocked-on-decision", "security-pending", "ready", "done"];

// Pipeline vocabulary a non-technical reader would not know.
const JARGON = /\b(ANALYZE|SECURITY|VERIFY|INGEST|GATE|pipeline|head|upstream|CODEOWNERS|diff|red→green|adversarial|canonical|dedup)\b/;
const MAX_MEANING = 140;

function entries(): [string, GlossaryEntry][] {
  const out: [string, GlossaryEntry][] = Object.entries(TERMS);
  for (const d of DISPOSITIONS) out.push([`disposition.${d}`, dispositionEntry(d)!]);
  for (const s of CLUSTER_STATES) out.push([`cluster.${s}`, clusterStateEntry(s)!]);
  for (const v of ["GREEN", "YELLOW", "RED", "not-run"] as const) out.push([`safety.${v}`, safetyEntry(v)!]);
  return out;
}

test("every glossary explanation is short", () => {
  for (const [key, e] of entries()) {
    assert.ok(e, `missing glossary entry ${key}`);
    for (const text of [e.meaning, e.triggers, e.note]) {
      if (text) assert.ok(text.length <= MAX_MEANING, `${key} is ${text.length} chars: ${text}`);
    }
  }
});

test("no glossary explanation uses pipeline jargon", () => {
  for (const [key, e] of entries()) {
    for (const text of [e.meaning, e.triggers, e.note]) {
      const hit = text?.match(JARGON) ?? null;
      assert.equal(hit, null, `${key} uses "${hit?.[0]}": ${text}`);
    }
  }
});
