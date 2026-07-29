import { useId, useState, type ReactNode } from "react";
import { TERMS, type GlossaryEntry } from "../glossary";
import { useExec } from "../ExecContext";

/** Instance-specific reason shown under the glossary meaning — e.g. the actual
 *  rationale for why THIS PR landed on its disposition. */
export interface InstanceWhy {
  rationale?: string | null;
  asks?: string[] | null;        // gap-closing asks (request-changes)
  stale?: boolean;               // the analysis behind this is stale
  tone?: "accent" | "danger";    // colour of the "why" block (danger for blocks)
  label?: string;                // defaults to "Why this PR"
}

const CARD_W = 320;

/** A rich hover/focus card explaining a coded term. Wraps any inline trigger
 *  content and shows the glossary `entry` plus an optional instance-specific
 *  `why`. The dotted-underline + ⓘ cue signals it's hoverable. Keyboard-
 *  accessible: focus opens it, blur or Esc closes it. Renders its children bare
 *  when there's no entry, so callers can pass a possibly-missing lookup.
 *
 *  `focusable` (default true) gives the wrapper its own tab stop — right for a
 *  static label or column header. Set it false when wrapping something already
 *  focusable (a button, a link) or a dense per-row cell: focus still bubbles up
 *  from an inner control so keyboard users see the card, without flooding the
 *  tab order with one stop per table row. */
export function InfoTip({ entry, why, children, cue = true, focusable = true, className = "", body }: {
  entry?: GlossaryEntry | null;
  why?: InstanceWhy | null;
  children: ReactNode;
  cue?: boolean;
  focusable?: boolean;
  className?: string;
  /** Custom popup content. When set, the card shows only this — no glossary
   *  title/meaning — for cells that want to show data, not a definition. */
  body?: ReactNode;
}) {
  const { botLogin } = useExec();
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const tipId = useId();
  if (!entry && !body) return <>{children}</>;
  const replaceBot = (text: string | undefined): string | undefined => text?.replaceAll("{bot}", botLogin);
  const renderedEntry = entry ? {
    ...entry,
    title: replaceBot(entry.title) ?? entry.title,
    meaning: replaceBot(entry.meaning) ?? entry.meaning,
    triggers: replaceBot(entry.triggers),
    note: replaceBot(entry.note),
  } : null;

  const open = (el: HTMLElement) => {
    const b = el.getBoundingClientRect();
    setPos({ x: b.left, y: b.bottom });
  };
  const shown = pos !== null;

  return (
    <span className={`glo-trigger ${cue ? "glo-cue" : ""} ${className}`}
      tabIndex={focusable ? 0 : undefined} aria-describedby={shown ? tipId : undefined}
      onMouseEnter={(e) => open(e.currentTarget)}
      onMouseLeave={() => setPos(null)}
      onFocus={(e) => open(e.currentTarget)}
      onBlur={() => setPos(null)}
      onKeyDown={(e) => { if (e.key === "Escape") setPos(null); }}>
      {children}
      {cue && <span className="glo-i" aria-hidden="true">ⓘ</span>}
      {shown && pos && (
        <div className="glo-pop" role="tooltip" id={tipId}
          style={{ left: Math.min(pos.x, window.innerWidth - CARD_W - 12), top: pos.y + 5 }}>
          {body ?? (renderedEntry && (
            <>
              <div className="glo-pop-title">{renderedEntry.title}</div>
              <div className="glo-pop-meaning">{renderedEntry.meaning}</div>
              {why?.rationale && (
                <div className={`glo-why glo-why-${why.tone ?? "accent"}`}>
                  <div className="glo-why-label">{why.label ?? "Why this PR"}</div>
                  <div className="glo-why-text">{why.rationale}</div>
                </div>
              )}
              {why?.asks && why.asks.length > 0 && (
                <div className="glo-asks">
                  <div className="glo-why-label">What's blocking</div>
                  <ul>{why.asks.map((a, i) => <li key={i}>{a}</li>)}</ul>
                </div>
              )}
              {why?.stale && (
                <div className="glo-stale">⟳ Stale — the PR head moved since this analysis ran.</div>
              )}
              {renderedEntry.triggers && !why?.rationale && (
                <div className="glo-pop-triggers">{renderedEntry.triggers}</div>
              )}
              {renderedEntry.note && <div className="glo-pop-note">{renderedEntry.note}</div>}
            </>
          ))}
        </div>
      )}
    </span>
  );
}

/** Static-term convenience: look up a glossary key and wrap text in an InfoTip.
 *  Falls back to the entry's own title when no children are given. */
export function Term({ k, children, cue = true }: { k: string; children?: ReactNode; cue?: boolean }) {
  const entry = TERMS[k] ?? null;
  return <InfoTip entry={entry} cue={cue}>{children ?? entry?.title ?? k}</InfoTip>;
}
