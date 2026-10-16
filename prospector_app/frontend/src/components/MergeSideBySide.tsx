import { Fragment } from "react";
import { mergeColumns, type CellLine, type MergedPart } from "./mergeColumns";

function Line({ line, tint }: { line: CellLine; tint: "add" | "del" }) {
  return (
    <div className={`m3-line${line.changed ? ` m3-${tint}` : ""}`}>
      <span className="m3-num">{line.num}</span>
      <span className="m3-code">{line.text || " "}</span>
    </div>
  );
}

function Merged({ part }: { part: MergedPart }) {
  if (part.kind === "line") return <Line line={part.line} tint="add" />;
  return (
    <div className="m3-conflict">
      <div className="m3-conflict-head">⚠ Unresolved — main and this PR both changed these lines</div>
      {part.base.length > 0 && (
        <>
          <div className="m3-conflict-sub">Before either change:</div>
          {part.base.map((t, i) => (
            <div className="m3-line" key={i}><span className="m3-num" /><span className="m3-code">{t || " "}</span></div>
          ))}
        </>
      )}
    </div>
  );
}

/** A conflict diff as three aligned columns: main, this PR, and the merged
 *  tree git left. Red marks main's lines the PR lacks; green marks the PR's,
 *  and the merged lines, that main lacks. */
export function MergeSideBySide({ diffText }: { diffText: string }) {
  const files = mergeColumns(diffText);
  if (files.length === 0) return <div className="muted pad">No conflicted files to lay out side by side — use “Merge diff”.</div>;
  return (
    <div className="m3">
      <div className="m3-head" title="The current base the rebase replays onto (with any PR commits already replayed)">Main</div>
      <div className="m3-head" title="The PR commit being replayed">This PR</div>
      <div className="m3-head m3-last" title="The tree git produced, before any resolution">Merged</div>
      {files.map((f) => (
        <Fragment key={f.path}>
          <div className="m3-band">📄 {f.path}</div>
          {f.hunks.map((h, hi) => (
            <Fragment key={hi}>
              <div className="m3-hunk">{hi > 0 ? "⋯ " : ""}{h.header}</div>
              {h.rows.map((r, ri) => r.kind === "context" ? (
                <Fragment key={ri}>
                  {[r.main, r.pr, r.merged].map((num, ci) => (
                    <div className={`m3-cell${ci === 2 ? " m3-last" : ""}`} key={ci}><Line line={{ num, text: r.text, changed: false }} tint="add" /></div>
                  ))}
                </Fragment>
              ) : (
                <Fragment key={ri}>
                  <div className="m3-cell">{r.main.map((l) => <Line key={l.num} line={l} tint="del" />)}</div>
                  <div className="m3-cell">{r.pr.map((l) => <Line key={l.num} line={l} tint="add" />)}</div>
                  <div className="m3-cell m3-last">{r.merged.map((p, i) => <Merged key={i} part={p} />)}</div>
                </Fragment>
              ))}
            </Fragment>
          ))}
        </Fragment>
      ))}
    </div>
  );
}
