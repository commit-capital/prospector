import { useState, type ReactNode } from "react";

/** One-line summary that expands to full detail on click. */
export function Collapsible({
  summary, children, defaultOpen = false, className = "", tone, action, id, openSignal,
}: {
  summary: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  className?: string;
  tone?: "green" | "yellow" | "red" | "muted";
  /** Control(s) shown at the right edge of the header row. Rendered as a
   *  sibling of the toggle button, so clicking them never toggles the body
   *  and interactive content (buttons, links) never nests inside a button. */
  action?: ReactNode;
  id?: string;
  /** Each new value opens the body. */
  openSignal?: number;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [signal, setSignal] = useState(openSignal);
  if (openSignal !== signal) {
    setSignal(openSignal);
    if (openSignal != null) setOpen(true);
  }
  return (
    <div id={id} className={`collapsible ${tone ? `tone-${tone}` : ""} ${className}`}>
      <div className="collapsible-row">
        <button className="collapsible-head" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          <span className={`caret ${open ? "open" : ""}`}>▸</span>
          <span className="collapsible-summary">{summary}</span>
        </button>
        {action && <span className="collapsible-action">{action}</span>}
      </div>
      {open && <div className="collapsible-body">{children}</div>}
    </div>
  );
}
