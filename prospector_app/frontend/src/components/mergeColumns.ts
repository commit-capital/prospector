/** A combined ("merge") diff split into three aligned columns — the paused
 *  rebase's HEAD (current base), the PR commit being replayed, and the working
 *  tree git left — so a conflict reads without decoding `diff --cc` prefixes
 *  or conflict markers.
 *
 *  Each line's two-column prefix says which sides hold it: on a `-` line a
 *  side holds it when its column is `-` (and the result does not); on any
 *  other line a side holds it unless its column is `+` (and the result does).
 *  Lines every side holds are context rows; each run of the rest is one change
 *  row, the three cells side by side. In the merged cell a conflict-marker
 *  region collapses to a conflict part carrying the common ancestor's lines
 *  (the `|||||||` section, empty without diff3 style). */

export interface CellLine {
  num: number;
  text: string;
  /** Main: the PR lacks it. PR and merged: main lacks it. */
  changed: boolean;
}

export type MergedPart =
  | { kind: "line"; line: CellLine }
  | { kind: "conflict"; base: string[] };

export type MergeRow =
  | { kind: "context"; text: string; main: number; pr: number; merged: number }
  | { kind: "change"; main: CellLine[]; pr: CellLine[]; merged: MergedPart[] };

export interface MergeHunk { header: string; rows: MergeRow[] }
export interface MergeFile { path: string; hunks: MergeHunk[] }

const HUNK = /^@@@ -(\d+)(?:,\d+)? -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@@ ?(.*)$/;

type ConflictSection = "ours" | "base" | "theirs" | null;

/** The diff's combined (`diff --cc` / `diff --combined`) files; any other file
 *  in it is skipped. */
export function mergeColumns(diffText: string): MergeFile[] {
  const files: MergeFile[] = [];
  let file: MergeFile | null = null;
  let hunk: MergeHunk | null = null;
  let n = { main: 0, pr: 0, merged: 0 };
  let section: ConflictSection = null;
  let conflict: { kind: "conflict"; base: string[] } | null = null;

  const changeRow = (): Extract<MergeRow, { kind: "change" }> => {
    const last = hunk!.rows[hunk!.rows.length - 1];
    if (last?.kind === "change") return last;
    const row: MergeRow = { kind: "change", main: [], pr: [], merged: [] };
    hunk!.rows.push(row);
    return row;
  };

  for (const raw of diffText.split("\n")) {
    if (raw.startsWith("diff ")) {
      const m = /^diff --(?:cc|combined) (.+)$/.exec(raw);
      file = m ? { path: m[1], hunks: [] } : null;
      if (file) files.push(file);
      hunk = null;
      section = null;
      continue;
    }
    if (!file) continue;
    const h = HUNK.exec(raw);
    if (h) {
      hunk = { header: h[4], rows: [] };
      file.hunks.push(hunk);
      n = { main: +h[1], pr: +h[2], merged: +h[3] };
      section = null;
      continue;
    }
    if (!hunk || raw.startsWith("\\")) continue;

    const c1 = raw[0] ?? " ";
    const c2 = raw[1] ?? " ";
    const text = raw.slice(2);
    const removed = c1 === "-" || c2 === "-";
    const inMain = removed ? c1 === "-" : c1 !== "+";
    const inPr = removed ? c2 === "-" : c2 !== "+";
    const inMerged = !removed;

    if (c1 === "+" && c2 === "+") {
      const marker = /^<{7}(?: |$)/.test(text) ? "ours"
        : /^\|{7}(?: |$)/.test(text) ? "base"
        : /^={7}$/.test(text) ? "theirs"
        : /^>{7}(?: |$)/.test(text) ? "end" : null;
      if (marker) {
        if (marker === "ours") {
          conflict = { kind: "conflict", base: [] };
          changeRow().merged.push(conflict);
        }
        section = marker === "end" ? null : marker;
        n.merged++;
        continue;
      }
    }

    if (inMain && inPr && inMerged && section === null) {
      hunk.rows.push({ kind: "context", text, main: n.main++, pr: n.pr++, merged: n.merged++ });
      continue;
    }

    const row = changeRow();
    if (inMain) row.main.push({ num: n.main++, text, changed: !inPr });
    if (inPr) row.pr.push({ num: n.pr++, text, changed: !inMain });
    if (inMerged) {
      if (section === "base") conflict!.base.push(text);
      else if (section === null) row.merged.push({ kind: "line", line: { num: n.merged, text, changed: !inMain } });
      n.merged++;
    }
  }
  return files;
}
