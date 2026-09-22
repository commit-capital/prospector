import { useEffect, useRef, useState } from "react";
import { COLUMNS } from "./columns";
import { Term } from "../InfoTip";
import { useExec } from "../../ExecContext";

// A dropdown menu toggling which non-anchor columns show — one button in the
// toolbar, with a checkbox per column behind it.
export function ColumnToggles({ isOn, toggle, reset }: {
  isOn: (k: string) => boolean;
  toggle: (k: string) => void;
  reset: () => void;
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
          {COLUMNS.filter((c) => !c.fixed && (!c.capability || activeReviewers(c.capability).length > 0)).map((c) => (
            <label key={c.key} className="col-menu-row">
              <input type="checkbox" checked={isOn(c.key)} onChange={() => toggle(c.key)} />
              {" "}{c.label}
            </label>
          ))}
          <button className="link-btn" onClick={reset}>Reset</button>
        </div>
      )}
    </div>
  );
}
