import {
  Children, Fragment, isValidElement, useEffect, useId, useLayoutEffect, useMemo, useRef, useState,
  type KeyboardEvent, type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { edge, step, typeahead, type SelectItem } from "../selectNav";

function text(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(text).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return text(node.props.children);
  return "";
}

type ChildProps = { value?: string | number; label?: string; disabled?: boolean; title?: string; children?: ReactNode };

function collect(children: ReactNode, group: string | null, out: SelectItem[]): SelectItem[] {
  Children.forEach(children, (child) => {
    if (!isValidElement<ChildProps>(child)) return;
    const p = child.props;
    if (child.type === "option") {
      const label = text(p.children);
      out.push({ value: p.value === undefined ? label : String(p.value), label, disabled: !!p.disabled, group, title: p.title });
    } else if (child.type === "optgroup") {
      collect(p.children, p.label ?? "", out);
    } else if (child.type === Fragment) {
      collect(p.children, group, out);
    }
  });
  return out;
}

const GAP = 4;
const MARGIN = 8;
const MAX_HEIGHT = 320;

/** The app's dropdown: written like a native `<select>` with `<option>` and
 *  `<optgroup>` children, rendered as a themed button and listbox. The menu is
 *  portaled to `<body>` so no scrolling or clipped container cuts it off, and
 *  its mousedowns stop at the menu so a host popout's outside-click handler
 *  does not read a pick as a click away. */
export function Select({ value, onChange, children, disabled = false, className, title, id, "aria-label": ariaLabel }: {
  value: string | number | undefined;
  onChange: (value: string) => void;
  children: ReactNode;
  disabled?: boolean;
  className?: string;
  title?: string;
  id?: string;
  "aria-label"?: string;
}) {
  const items = useMemo(() => collect(children, null, []), [children]);
  const current = value === undefined ? -1 : items.findIndex((it) => it.value === String(value));
  const shown = items[current >= 0 ? current : 0];
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLUListElement>(null);
  const typed = useRef({ query: "", at: 0 });
  const listId = useId();

  const show = () => {
    setActive(current >= 0 && !items[current].disabled ? current : edge(items, "first"));
    setOpen(true);
  };
  const choose = (i: number) => {
    setOpen(false);
    const it = items[i];
    if (it && !it.disabled && i !== current) onChange(it.value);
  };

  useLayoutEffect(() => {
    const menu = menuRef.current;
    const trigger = triggerRef.current;
    if (!open || !menu || !trigger) return;
    const r = trigger.getBoundingClientRect();
    const below = window.innerHeight - r.bottom - GAP - MARGIN;
    const above = r.top - GAP - MARGIN;
    const down = below >= Math.min(MAX_HEIGHT, menu.scrollHeight) || below >= above;
    menu.style.minWidth = `${r.width}px`;
    menu.style.maxHeight = `${Math.min(MAX_HEIGHT, down ? below : above)}px`;
    menu.style.left = `${Math.max(MARGIN, Math.min(r.left, window.innerWidth - menu.offsetWidth - MARGIN))}px`;
    menu.style.top = down ? `${r.bottom + GAP}px` : `${r.top - GAP - menu.offsetHeight}px`;
  }, [open]);

  useEffect(() => {
    if (open) menuRef.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [open, active]);

  useEffect(() => {
    if (!open) return;
    const away = (e: Event) => {
      const t = e.target as Node;
      if (!menuRef.current?.contains(t) && !triggerRef.current?.contains(t)) setOpen(false);
    };
    const close = () => setOpen(false);
    document.addEventListener("mousedown", away);
    document.addEventListener("scroll", away, true);
    window.addEventListener("resize", close);
    window.addEventListener("blur", close);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("scroll", away, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("blur", close);
    };
  }, [open]);

  const onKey = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); show(); }
      return;
    }
    switch (e.key) {
      case "ArrowDown": e.preventDefault(); setActive(step(items, active, 1)); return;
      case "ArrowUp": e.preventDefault(); setActive(step(items, active, -1)); return;
      case "Home": case "PageUp": e.preventDefault(); setActive(edge(items, "first")); return;
      case "End": case "PageDown": e.preventDefault(); setActive(edge(items, "last")); return;
      case "Enter": case " ":
        if (e.key === " " && typed.current.query && Date.now() - typed.current.at < 500) break;
        e.preventDefault(); choose(active); return;
      case "Escape": e.preventDefault(); e.stopPropagation(); setOpen(false); return;
      case "Tab": setOpen(false); return;
    }
    if (e.key.length !== 1 || e.metaKey || e.ctrlKey || e.altKey) return;
    const now = Date.now();
    const query = (now - typed.current.at < 500 ? typed.current.query : "") + e.key;
    typed.current = { query, at: now };
    const hit = typeahead(items, query, query.length > 1 ? active - 1 : active);
    if (hit >= 0) setActive(hit);
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        role="combobox"
        id={id}
        className={`ui-select${className ? ` ${className}` : ""}`}
        title={title}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-activedescendant={open && active >= 0 ? `${listId}-${active}` : undefined}
        disabled={disabled}
        onClick={() => (open ? setOpen(false) : show())}
        onKeyDown={onKey}
      >
        <span className="ui-select-value">{shown?.label ?? ""}</span>
        <svg className="ui-select-chevron" viewBox="0 0 16 16" aria-hidden="true">
          <path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open && createPortal(
        <ul ref={menuRef} id={listId} role="listbox" className="ui-select-menu" aria-label={ariaLabel ?? title}
          onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); }}>
          {items.map((it, i) => {
            const header = it.group !== null && it.group !== items[i - 1]?.group
              ? <li role="presentation" className="ui-select-group">{it.group}</li>
              : null;
            return (
              <Fragment key={i}>
                {header}
                <li id={`${listId}-${i}`} role="option" data-index={i} data-value={it.value} title={it.title}
                  aria-selected={i === current} aria-disabled={it.disabled || undefined}
                  className={`ui-select-option${i === active ? " is-active" : ""}${it.group !== null ? " in-group" : ""}`}
                  onMouseMove={() => { if (!it.disabled && i !== active) setActive(i); }}
                  onClick={() => choose(i)}>
                  <span className="ui-select-check" aria-hidden="true">{i === current ? "✓" : ""}</span>
                  {it.label}
                </li>
              </Fragment>
            );
          })}
        </ul>,
        document.body,
      )}
    </>
  );
}
