import assert from "node:assert/strict";
import { test } from "node:test";
import { isMuted, mute, readMutes } from "./mute.ts";

function memory(): Storage {
  const m = new Map<string, string>();
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => { m.set(k, v); } } as Storage;
}
const H = 3_600_000;

test("a mute lasts 24 hours", () => {
  const s = memory();
  mute("offline:lap", 0, () => s);
  const m = readMutes(() => s);
  assert.equal(isMuted("offline:lap", m, 23 * H), true);
  assert.equal(isMuted("offline:lap", m, 25 * H), false);
  assert.equal(isMuted("pins", m, 1), false);
});

test("blocked storage mutes nothing and never throws", () => {
  const broken = () => { throw new Error("blocked"); };
  assert.doesNotThrow(() => mute("pins", 0, broken));
  assert.deepEqual(readMutes(broken), {});
});
