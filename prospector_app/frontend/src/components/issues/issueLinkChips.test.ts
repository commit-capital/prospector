import assert from "node:assert/strict";
import { test } from "node:test";
import type { IssuePR } from "../../api";
import { EVIDENCE, REFERENCED, linkStateChip } from "./issueLinkChips.ts";

const pr = (extra: Partial<IssuePR> = {}): IssuePR => ({ pr: 1, ...extra });

test("every referenced kind has an evidence line to put in its tooltip", () => {
  assert.deepEqual([...REFERENCED].sort(), Object.keys(EVIDENCE).sort());
});

test("a resolved PR chips its state", () => {
  assert.deepEqual(linkStateChip(pr({ state: "merged" })), { label: "merged", cls: "chip-purple" });
  assert.deepEqual(linkStateChip(pr({ state: "closed" })), { label: "closed", cls: "chip-muted" });
});

test("an open draft chips as draft and an ordinary open PR chips not at all", () => {
  assert.deepEqual(linkStateChip(pr({ state: "open", draft: true })),
    { label: "draft", cls: "chip-muted" });
  assert.equal(linkStateChip(pr({ state: "open" })), null);
  assert.equal(linkStateChip(pr({ state: null, draft: true })), null);
});
