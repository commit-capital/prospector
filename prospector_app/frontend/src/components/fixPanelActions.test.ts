import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Every single-purpose action a Checks row offers is also in the Other actions panel.
const tsx = readFileSync(new URL("./FixPanel.tsx", import.meta.url), "utf8");

test("the Other actions panel offers re-test, resolve conflicts, author a fix, and rewrite", () => {
  assert.match(tsx, /\(\["update", "rebase", "fix", "describe"\] as FixAction\[\]\)/);
});
