/** Line classification for git diffs rendered as raw text — the fallback for
 *  formats react-diff-view's parser rejects, chiefly the combined ("merge")
 *  diff a paused rebase produces (`diff --cc`, `@@@` hunk headers, two-column
 *  line prefixes). Classifying per line lets the fallback paint additions
 *  green and deletions red the way the parsed view does (#94). */

export type RawDiffLineKind = "add" | "del" | "hunk" | "meta" | "context";

export interface RawDiffLine {
  text: string;
  kind: RawDiffLineKind;
}

/** Split a git diff into lines, each tagged add / del / hunk / meta / context.
 *  Handles both single-column unified diffs and the two-column combined
 *  format: a `diff --cc` / `diff --combined` header (or an `@@@` hunk) puts a
 *  file into two-column mode, where a line is a deletion if either parent
 *  column carries `-` and an addition if either carries `+`. Lines outside a
 *  hunk (`diff` / `index` / `--- a/f` / `+++ b/f` / mode lines) are meta, so
 *  file headers never read as changes while an in-hunk line whose content
 *  starts with `--` or `++` still does. */
export function classifyRawDiff(diffText: string): RawDiffLine[] {
  let cols: 1 | 2 = 1;
  let inHunk = false;
  return diffText.split("\n").map((text): RawDiffLine => {
    if (text.startsWith("diff ")) {
      cols = text.startsWith("diff --cc") || text.startsWith("diff --combined") ? 2 : 1;
      inHunk = false;
      return { text, kind: "meta" };
    }
    if (text.startsWith("@@")) {
      if (text.startsWith("@@@")) cols = 2;
      inHunk = true;
      return { text, kind: "hunk" };
    }
    if (!inHunk) {
      return { text, kind: text.trim() === "" ? "context" : "meta" };
    }
    if (text.startsWith("\\")) {
      return { text, kind: "meta" };
    }
    const prefix = text.slice(0, cols);
    if (prefix.includes("-")) return { text, kind: "del" };
    if (prefix.includes("+")) return { text, kind: "add" };
    return { text, kind: "context" };
  });
}
