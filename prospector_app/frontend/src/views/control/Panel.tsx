import { useState, type ReactNode } from "react";
import { readFold, writeFold } from "./fold";

/** One Control-tab section: a bordered block with a shaded header band. A
 *  foldable panel shows `summary` in its header while folded and remembers,
 *  per browser, whether it was left open. */
export function Panel({ id, title, meta, summary, tone, foldable = false, defaultOpen = true, action, children }: {
  id: string;
  title: string;
  meta?: ReactNode;
  summary?: ReactNode;
  tone?: "danger" | "accent";
  foldable?: boolean;
  defaultOpen?: boolean;
  action?: ReactNode;
  children: ReactNode;
}) {
  const [open, setOpen] = useState<boolean>(() => !foldable || readFold(id, defaultOpen));
  const toggle = () => setOpen((o) => { writeFold(id, !o); return !o; });
  const head = (
    <>
      {foldable && <span className={`caret ${open ? "open" : ""}`}>▸</span>}
      <h3 className="cpanel-title">{title}</h3>
      {(open || !summary) && meta != null && <span className="cpanel-meta">{meta}</span>}
      {!open && summary != null && <span className="cpanel-meta">{summary}</span>}
    </>
  );
  return (
    <section className={`cpanel${tone ? ` tone-${tone}` : ""}${open ? "" : " folded"}`} aria-label={title}>
      <div className="cpanel-head">
        {foldable
          ? <button className="cpanel-fold" aria-expanded={open} onClick={toggle}>{head}</button>
          : head}
        {action && <span className="cpanel-action">{action}</span>}
      </div>
      {open && <div className="cpanel-body">{children}</div>}
    </section>
  );
}
