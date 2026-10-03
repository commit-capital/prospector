import assert from "node:assert/strict";
import { test } from "node:test";
import { readFold, writeFold } from "./fold.ts";

function memory(): Storage {
  const m = new Map<string, string>();
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => { m.set(k, v); } } as Storage;
}

test("fold state falls back when storage throws", () => {
  const broken = () => { throw new Error("blocked"); };
  assert.equal(readFold("jobs", true, broken), true);
  assert.equal(readFold("jobs", false, broken), false);
  assert.doesNotThrow(() => writeFold("jobs", true, broken));
});

test("a written fold reads back", () => {
  const s = memory();
  assert.equal(readFold("capacity", false, () => s), false);
  writeFold("capacity", true, () => s);
  assert.equal(readFold("capacity", false, () => s), true);
  writeFold("capacity", false, () => s);
  assert.equal(readFold("capacity", true, () => s), false);
});
