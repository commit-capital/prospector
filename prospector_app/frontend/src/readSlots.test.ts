import assert from "node:assert/strict";
import { test } from "node:test";
import { READ_SLOTS, withReadSlot } from "./readSlots.ts";

const settle = (): Promise<void> => new Promise<void>((resolve) => setImmediate(resolve));

test("reads past the slot count wait, and start in the order they were asked for", async () => {
  const started: number[] = [];
  const finish: (() => void)[] = [];
  const reads = Array.from({ length: READ_SLOTS + 2 }, (_, i) =>
    withReadSlot(() => {
      started.push(i);
      return new Promise<number>((resolve) => { finish[i] = () => resolve(i); });
    }));
  await settle();
  assert.deepEqual(started, [0, 1, 2]);

  finish[1]();
  await settle();
  assert.deepEqual(started, [0, 1, 2, 3]);

  finish[0]();
  finish[2]();
  finish[3]();
  await settle();
  assert.deepEqual(started, [0, 1, 2, 3, 4]);
  finish[4]();
  assert.deepEqual(await Promise.all(reads), [0, 1, 2, 3, 4]);
});

test("a failed read gives its slot back", async () => {
  const failures = Array.from({ length: READ_SLOTS }, () =>
    withReadSlot(() => Promise.reject(new Error("unreachable"))));
  for (const failure of failures) await assert.rejects(failure, /unreachable/);

  let ran = 0;
  await Promise.all(Array.from({ length: READ_SLOTS }, () =>
    withReadSlot(async () => { ran += 1; })));
  assert.equal(ran, READ_SLOTS);
});
