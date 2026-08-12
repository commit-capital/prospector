import { useEffect, useMemo, useRef, useState } from "react";
import { parseDiff, Diff, Hunk, getChangeKey, type HunkData, type ChangeData, type ChangeEventArgs } from "react-diff-view";
import "react-diff-view/style/index.css";
import { useAgentPane } from "./AgentPane";
import { classifyRawDiff } from "./rawDiffLines";

/** Per-line colored rendering for diffs react-diff-view cannot parse — the
 *  combined format of a merge/conflict diff most of all. Each line is a block
 *  span so its add/del tint spans the full row width. */
function RawDiff({ diffText }: { diffText: string }) {
  return (
    <pre className="rawdiff">
      {classifyRawDiff(diffText).map((l, i) => (
        <span key={i} className={`rawdiff-line rawdiff-${l.kind}`}>{l.text || " "}</span>
      ))}
    </pre>
  );
}

interface Menu { x: number; y: number; file: string; line: number; key: string }

/** Unified diff with a GitHub-style per-line popup: click a line to ask the
 *  agent to explain it, ask a question, or leave a comment.
 *  Ctrl/⌘-click any line (changed or context) jumps to it on GitHub (#18). */
export function DiffView({ diffText, onComment, blobUrl }: {
  diffText: string;
  onComment?: (file: string, line: number) => void;
  blobUrl?: (file: string, line: number) => string | null;
}) {
  const { askAbout } = useAgentPane();
  const [menu, setMenu] = useState<Menu | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const mouse = useRef({ x: 0, y: 0, mod: false });

  const files = useMemo(() => {
    try { return parseDiff(diffText || ""); } catch { return []; }
  }, [diffText]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setMenu(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!diffText) return <div className="muted pad">No diff available.</div>;
  if (files.length === 0) return <RawDiff diffText={diffText} />;

  const openMenu = (file: string, change: ChangeData) => {
    const ln = "newLineNumber" in change ? change.newLineNumber : change.lineNumber;
    if (ln == null) return;
    // Ctrl/⌘-click → jump straight to the line on GitHub instead of the menu.
    if (mouse.current.mod && blobUrl) {
      const url = blobUrl(file, ln);
      if (url) { window.open(url, "_blank", "noopener"); return; }
    }
    const key = getChangeKey(change);
    setSelected(key);
    const x = Math.min(mouse.current.x, window.innerWidth - 220);
    const y = Math.min(mouse.current.y, window.innerHeight - 140);
    setMenu({ x, y, file, line: ln, key });
  };

  return (
    <div className={`diffview ${blobUrl ? "diffview-linkable" : ""}`}
      onMouseDownCapture={(e) => { mouse.current = { x: e.clientX, y: e.clientY, mod: e.metaKey || e.ctrlKey }; }}>
      {files.map((file, i) => {
        const path = file.newPath || file.oldPath || `file-${i}`;
        return (
          <div className="difffile" key={path}>
            <div className="difffile-head">{path}</div>
            <Diff
              viewType="unified"
              diffType={file.type}
              hunks={file.hunks}
              selectedChanges={selected ? [selected] : []}
              gutterEvents={{ onClick: (e: ChangeEventArgs) => e.change && openMenu(path, e.change) }}
              codeEvents={{ onClick: (e: ChangeEventArgs) => e.change && openMenu(path, e.change) }}
            >
              {(hunks: HunkData[]) => hunks.map((h) => <Hunk key={h.content} hunk={h} />)}
            </Diff>
          </div>
        );
      })}

      {menu && (
        <>
          <div className="diffmenu-scrim" onClick={() => setMenu(null)} />
          <div className="diffmenu" style={{ left: menu.x, top: menu.y }}>
            <div className="diffmenu-loc">{menu.file.split("/").pop()}:{menu.line}</div>
            <button onClick={() => { askAbout(menu.file, menu.line, "Explain this code to me, briefly."); setMenu(null); }}>🤖 Explain this code</button>
            <button onClick={() => { askAbout(menu.file, menu.line); setMenu(null); }}>💬 Ask the agent…</button>
            {onComment && <button onClick={() => { onComment(menu.file, menu.line); setMenu(null); }}>📝 Comment on this line</button>}
          </div>
        </>
      )}
    </div>
  );
}
