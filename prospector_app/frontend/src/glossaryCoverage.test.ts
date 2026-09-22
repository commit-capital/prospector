import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { term } from "./glossary.ts";
import { LANES } from "./components/explorer/lanes.ts";

// The column registry is a .tsx module the type-stripping test runner cannot
// import, so its term keys are read off the source text instead.
const columnsSource = readFileSync(new URL("./components/explorer/columns.tsx", import.meta.url), "utf8");

test("every Explorer column header term has a glossary entry", () => {
  const keys = [...columnsSource.matchAll(/term: "([^"]+)"/g)].map((m) => m[1]);
  assert.ok(keys.length >= 15, `expected the column registry's term keys, found ${keys.length}`);
  for (const key of keys) {
    assert.ok(term(key), `glossary.ts has no entry for column term "${key}"`);
  }
});

test("every Explorer lane chip has a glossary entry", () => {
  for (const lane of LANES) {
    assert.ok(term(`lane.${lane.key}`), `glossary.ts has no entry for "lane.${lane.key}"`);
  }
});
