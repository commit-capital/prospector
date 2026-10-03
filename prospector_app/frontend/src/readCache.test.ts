import assert from "node:assert/strict";
import { test } from "node:test";
import { defineRead, MAX_ANSWERS, peekRead, runRead } from "./readCache.ts";

test("a read has no answer to peek at until one lands", async () => {
  const read = defineRead("first", async () => ({ total: 3 }));
  assert.equal(peekRead(read), undefined);
  assert.deepEqual(await runRead(read), { total: 3 });
  assert.deepEqual(peekRead(read), { total: 3 });
});

test("reads share an answer by key", async () => {
  await runRead(defineRead("shared", async () => "prefetched"));
  assert.equal(peekRead(defineRead("shared", async () => "never run")), "prefetched");
});

test("a failed read keeps the answer before it", async () => {
  await runRead(defineRead("flaky", async () => 1));
  await assert.rejects(runRead(defineRead("flaky", () => Promise.reject(new Error("down")))), /down/);
  assert.equal(peekRead(defineRead("flaky", async () => 0)), 1);
});

test("past MAX_ANSWERS the least recently answered read is dropped", async () => {
  const reads = Array.from({ length: MAX_ANSWERS + 1 }, (_, i) =>
    defineRead(`lru-${i}`, async () => i));
  for (const read of reads.slice(0, MAX_ANSWERS)) await runRead(read);
  await runRead(reads[0]);
  await runRead(reads[MAX_ANSWERS]);
  assert.equal(peekRead(reads[0]), 0);
  assert.equal(peekRead(reads[1]), undefined);
  assert.equal(peekRead(reads[MAX_ANSWERS]), MAX_ANSWERS);
});
