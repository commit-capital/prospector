import { useEffect, useRef, useState } from "react";
import type { ColumnMeta } from "./columns";
import { Term } from "../InfoTip";
import { useExec } from "../../ExecContext";

// A dropdown menu toggling which non-anchor columns show — one button in the
// toolbar, with a checkbox per column behind it — and, for a table that clips
// long text to one line, whether that text wraps.
export function ColumnToggles({ columns, isOn, toggle, reset, wrap, setWrap }: {
  columns: readonly ColumnMeta[];
  isOn: (k: string) => boolean;
  toggle: (k: string) => void;
  reset: () => void;
  wrap?: boolean;
  setWrap?: (on: boolean) => void;
}) {
  const { activeReviewers } = useExec();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  return (
    <div className="col-menu" ref={ref}>
      <button className={`chip toggle ${open ? "on" : ""}`} onClick={() => setOpen(!open)}
        aria-haspopup="menu" aria-expanded={open}>
        <Term k="ui.columns" cue={false}>Columns</Term> ▾
      </button>
      {open && (
        <div className="col-menu-dropdown" role="menu">
          {columns.filter((c) => !c.fixed && (!c.capability || activeReviewers(c.capability).length > 0)).map((c) => (
            <label key={c.key} className="col-menu-row">
              <input type="checkbox" checked={isOn(c.key)} onChange={() => toggle(c.key)} />
              {" "}{c.label}
            </label>
          ))}
          {setWrap && (
            <label className="col-menu-row col-menu-wrap"
              title="Let titles, statuses and summaries wrap onto more lines so each row shows all of its text">
              <input type="checkbox" checked={!!wrap} onChange={() => setWrap(!wrap)} />
              {" "}Wrap long text
            </label>
          )}
          <button className="link-btn" onClick={reset}>Reset</button>
        </div>
      )}
    </div>
  );
}
