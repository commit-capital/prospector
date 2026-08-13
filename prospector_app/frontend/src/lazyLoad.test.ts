import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import { loadWithRecovery } from "./lazyLoad.ts";

const values = new Map<string, string>();
let reloads = 0;

beforeEach(() => {
  values.clear();
  reloads = 0;
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { reload: () => { reloads += 1; } },
      sessionStorage: {
        getItem: (key: string): string | null => values.get(key) ?? null,
        removeItem: (key: string): void => { values.delete(key); },
        setItem: (key: string, value: string): void => { values.set(key, value); },
      },
    },
  });
});

afterEach(() => {
  Reflect.deleteProperty(globalThis, "window");
});

test("a rejected lazy import reloads the page once", async () => {
  void loadWithRecovery("pr-flyout", async () => {
    throw new Error("fetch failed");
  });
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(reloads, 1);

  await assert.rejects(
    loadWithRecovery("pr-flyout", async () => {
      throw new Error("still failing");
    }),
    /still failing/,
  );
  assert.equal(reloads, 1);
});

test("a successful lazy import clears its reload marker", async () => {
  values.set("prospector:lazy-load-reload:pr-flyout", "1");
  const loaded = await loadWithRecovery("pr-flyout", async () => ({ value: 42 }));
  assert.deepEqual(loaded, { value: 42 });
  assert.equal(values.size, 0);
  assert.equal(reloads, 0);
});
