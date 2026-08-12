import assert from "node:assert/strict";
import { test } from "node:test";
import { classifyRawDiff, type RawDiffLineKind } from "./rawDiffLines.ts";

function kinds(diff: string): RawDiffLineKind[] {
  return classifyRawDiff(diff).map((l) => l.kind);
}

const COMBINED = `diff --cc pipeline/gates.py
index 1111111,2222222..0000000
--- a/pipeline/gates.py
+++ b/pipeline/gates.py
@@@ -10,7 -10,7 +10,11 @@@ def pr_clean(pr)
  def merge_allowed(pr):
-     if pr.old_check():
 -    if pr.other_check():
++<<<<<<< HEAD
++    if pr.old_check():
++=======
++    if pr.other_check():
++>>>>>>> feature
          return False
      return True`;

test("a combined (merge) diff gets add/del/hunk/meta lines", () => {
  assert.deepEqual(kinds(COMBINED), [
    "meta",     // diff --cc
    "meta",     // index
    "meta",     // --- a/…
    "meta",     // +++ b/…
    "hunk",     // @@@ … @@@
    "context",  //   def merge_allowed(pr):
    "del",      // -     if pr.old_check():
    "del",      //  -    if pr.other_check():
    "add",      // ++<<<<<<< HEAD
    "add",      // ++    if pr.old_check():
    "add",      // ++=======
    "add",      // ++    if pr.other_check():
    "add",      // ++>>>>>>> feature
    "context",  //           return False
    "context",  //       return True
  ]);
});

test("combined mode is entered by an @@@ hunk even without a diff --cc header", () => {
  const diff = "@@@ -1,2 -1,2 +1,2 @@@\n +only in one parent\n- gone from one parent";
  assert.deepEqual(kinds(diff), ["hunk", "add", "del"]);
});

const UNIFIED = `diff --git a/x.py b/x.py
index 1234567..89abcde 100644
--- a/x.py
+++ b/x.py
@@ -1,3 +1,3 @@
 context line
-removed line
+added line`;

test("a plain unified diff classifies the same way the parsed view colors it", () => {
  assert.deepEqual(kinds(UNIFIED), [
    "meta", "meta", "meta", "meta", "hunk", "context", "del", "add",
  ]);
});

test("a diff --git file after a diff --cc file returns to one-column prefixes", () => {
  const diff = `${COMBINED}\n${UNIFIED}`;
  const twoCol = kinds(COMBINED);
  const oneCol = kinds(UNIFIED);
  assert.deepEqual(kinds(diff), [...twoCol, ...oneCol]);
  // One-column mode reads " -foo" as context, not a deletion.
  assert.deepEqual(kinds(`${UNIFIED}\n -dashed context`).at(-1), "context");
});

test("in-hunk lines whose content starts with dashes or pluses stay changes", () => {
  const diff = "@@ -1,2 +1,2 @@\n--- not a file header\n+++ not one either";
  assert.deepEqual(kinds(diff), ["hunk", "del", "add"]);
});

test("file headers outside a hunk are meta, never changes", () => {
  assert.deepEqual(kinds("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b"),
    ["meta", "meta", "hunk", "del", "add"]);
});

test("the no-newline marker is meta inside a hunk", () => {
  const diff = "@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file";
  assert.deepEqual(kinds(diff), ["hunk", "del", "add", "meta"]);
});

test("blank lines are context everywhere", () => {
  assert.deepEqual(kinds("diff --git a/x b/x\n\n@@ -1 +1 @@\n"),
    ["meta", "context", "hunk", "context"]);
});
