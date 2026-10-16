import assert from "node:assert/strict";
import { test } from "node:test";
import { mergeColumns, type MergeRow } from "./mergeColumns.ts";

const DIFF = `diff --cc scripts/provision.test.mjs
index bbe44698,b81c9fd8..00000000
--- a/scripts/provision.test.mjs
+++ b/scripts/provision.test.mjs
@@@ -102,4 -77,3 +102,4 @@@ process.exit(0)
    return baseCwd;
- function runProvision(baseCwd, { pathPrefix } = {}) {
+ function runProvision(baseCwd, { pathPrefix, publicUrl } = {}) {
    const worktreeCwd = makeTempDir();
 +  const appHome = makeInstanceHome();
@@@ -116,5 -90,4 +116,12 @@@
      APP_WORKTREES_DIR: worktreesHome,
++<<<<<<< HEAD
 +    APP_HOME: appHome,
++||||||| parent of ecb183db (fix)
++    APP_HOME: path.join(worktreesHome, "x"),
++=======
+     APP_HOME: path.join(worktreesHome, "x"),
+     ...(publicUrl ? { URL: publicUrl } : {}),
++>>>>>>> ecb183db (fix)
    },`;

const rows = (): MergeRow[][] => mergeColumns(DIFF)[0].hunks.map((h) => h.rows);

test("a cleanly merged change splits by side, numbered from the hunk header", () => {
  const [first] = rows();
  assert.deepEqual(first[0], { kind: "context", text: "  return baseCwd;", main: 102, pr: 77, merged: 102 });
  const change = first[1];
  assert.equal(change.kind, "change");
  if (change.kind !== "change") return;
  assert.deepEqual(change.main, [{ num: 103, text: "function runProvision(baseCwd, { pathPrefix } = {}) {", changed: true }]);
  assert.deepEqual(change.pr, [{ num: 78, text: "function runProvision(baseCwd, { pathPrefix, publicUrl } = {}) {", changed: true }]);
  assert.deepEqual(change.merged, [{ kind: "line", line: { num: 103, text: "function runProvision(baseCwd, { pathPrefix, publicUrl } = {}) {", changed: true } }]);
  const mainOnly = first[3];
  assert.equal(mainOnly.kind, "change");
  if (mainOnly.kind !== "change") return;
  assert.deepEqual(mainOnly.pr, []);
  assert.deepEqual(mainOnly.main, [{ num: 105, text: "  const appHome = makeInstanceHome();", changed: true }]);
  assert.deepEqual(mainOnly.merged, [{ kind: "line", line: { num: 105, text: "  const appHome = makeInstanceHome();", changed: false } }]);
});

test("a conflict region leaves each side its version and the merged cell the ancestor", () => {
  const [, second] = rows();
  assert.equal(second.length, 3);
  const change = second[1];
  assert.equal(change.kind, "change");
  if (change.kind !== "change") return;
  assert.deepEqual(change.main.map((l) => l.text), ["    APP_HOME: appHome,"]);
  assert.deepEqual(change.pr.map((l) => [l.num, l.text]), [
    [91, "    APP_HOME: path.join(worktreesHome, \"x\"),"],
    [92, "    ...(publicUrl ? { URL: publicUrl } : {}),"],
  ]);
  assert.deepEqual(change.merged, [{ kind: "conflict", base: ["    APP_HOME: path.join(worktreesHome, \"x\"),"] }]);
  assert.deepEqual(second[2], { kind: "context", text: "  },", main: 118, pr: 93, merged: 125 });
});

test("a diff with no combined file yields nothing", () => {
  assert.deepEqual(mergeColumns("diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b"), []);
});
