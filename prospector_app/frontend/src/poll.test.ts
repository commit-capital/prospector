import assert from "node:assert/strict";
import { afterEach, beforeEach, mock, test } from "node:test";
import { startPoll } from "./poll.ts";

const MS = 1_000;
let hidden = false;
const listeners = new Set<() => void>();

const settle = (): Promise<void> => new Promise<void>((resolve) => setImmediate(resolve));

function show(): void {
  hidden = false;
  for (const l of listeners) l();
}

beforeEach(() => {
  hidden = false;
  listeners.clear();
  mock.timers.enable({ apis: ["setTimeout"] });
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      get hidden(): boolean { return hidden; },
      addEventListener: (_: string, l: () => void): void => { listeners.add(l); },
      removeEventListener: (_: string, l: () => void): void => { listeners.delete(l); },
    },
  });
});

afterEach(() => {
  mock.timers.reset();
  Reflect.deleteProperty(globalThis, "document");
});

test("the next run waits for the last one to settle", async () => {
  let calls = 0;
  let finish = (): void => undefined;
  const stop = startPoll(() => {
    calls += 1;
    return new Promise<void>((resolve) => { finish = resolve; });
  }, MS);
  assert.equal(calls, 1);

  mock.timers.tick(MS * 5);
  assert.equal(calls, 1);

  finish();
  await settle();
  mock.timers.tick(MS);
  assert.equal(calls, 2);
  stop();
});

test("a failed run still schedules the next", async () => {
  let calls = 0;
  const stop = startPoll(() => {
    calls += 1;
    return Promise.reject(new Error("backend down"));
  }, MS);
  await settle();
  mock.timers.tick(MS);
  assert.equal(calls, 2);
  stop();
});

test("a run that falls due while the page is hidden waits until it is shown", async () => {
  let calls = 0;
  hidden = true;
  const stop = startPoll(async () => { calls += 1; }, MS);
  mock.timers.tick(MS * 5);
  assert.equal(calls, 0);

  show();
  assert.equal(calls, 1);
  stop();
});

test("showing the page before a run is due runs nothing extra", async () => {
  let calls = 0;
  const stop = startPoll(async () => { calls += 1; }, MS);
  await settle();
  show();
  assert.equal(calls, 1);
  stop();
});

test("a stopped poll runs no more", async () => {
  let calls = 0;
  const stop = startPoll(async () => { calls += 1; }, MS);
  await settle();
  stop();
  mock.timers.tick(MS * 5);
  show();
  assert.equal(calls, 1);
  assert.equal(listeners.size, 0);
});
