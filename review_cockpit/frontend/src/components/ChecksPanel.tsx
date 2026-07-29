import type { ReactNode } from "react";
import type { ChecksRollup, CheckItem } from "../api";
import { localDateTime, timeAgo } from "../timeAgo";
import { Collapsible } from "./Collapsible";

const ICON = { pass: "✓", warn: "⚠", fail: "✗", na: "–" } as const;
const TONE: Record<CheckItem["status"], "green" | "yellow" | "red" | "muted"> = {
  pass: "green", warn: "yellow", fail: "red", na: "muted",
};

function CheckRowLine({ chk, action }: { chk: CheckItem; action?: ReactNode }) {
  return (
    <span className="check-row-line">
      <span className={`check-ico st-${chk.status}`} aria-hidden="true">{ICON[chk.status]}</span>
      <span className="check-name">{chk.name}</span>
      <span className="check-detail muted small">{chk.detail}</span>
      <span className="check-age muted small" title={chk.at ? `checked ${localDateTime(chk.at)}` : "no timestamp recorded"}>
        {chk.at ? timeAgo(chk.at) : "—"}
      </span>
      {action && <span className="check-action">{action}</span>}
    </span>
  );
}

/** #550/#581: the topline surface — every check this deployment could run on
 *  this PR, its current status, the action that unblocks it (re-run, queue,
 *  re-trigger), right next to the status, and — when there's more to say than
 *  the one-line summary carries — an expandable toggle for the detail. */
export function ChecksPanel({ c, actions, bodies }: {
  c: ChecksRollup | undefined;
  /** per-check-key control(s) rendered inline with that row (e.g. "↻ Re-run"). */
  actions?: Partial<Record<string, ReactNode>>;
  /** per-check-key expanded detail; a row with no body isn't expandable. */
  bodies?: Partial<Record<string, ReactNode>>;
}) {
  if (!c || c.checks.length === 0) return null;
  const ratio = c.total > 0 ? c.passed / c.total : null;
  const tone = ratio == null ? "muted" : ratio === 1 ? "green" : ratio >= 0.6 ? "amber" : "red";
  return (
    <section className={`checks-panel checks-panel-${tone}`}>
      <div className="checks-panel-head">
        <h3>Checks</h3>
        <span className={`chip chip-${tone === "amber" ? "amber" : tone} sm`}>
          {ratio == null ? "none run yet" : `${c.passed}/${c.total} passed`}
        </span>
      </div>
      <div className="checks-panel-grid">
        {c.checks.map((chk) => {
          const action = actions?.[chk.key];
          const body = bodies?.[chk.key];
          if (!body) {
            return (
              <div key={chk.key} className={`check-row check-${chk.status}`}>
                <CheckRowLine chk={chk} action={action} />
              </div>
            );
          }
          // The summary renders inside the header <button>, so the action —
          // itself a button — goes in the `action` slot, a sibling of it
          // (HTML forbids a button inside a button).
          return (
            <Collapsible key={chk.key} className={`check-row-collapsible check-${chk.status}`}
              tone={TONE[chk.status]} summary={<CheckRowLine chk={chk} />} action={action}>
              {body}
            </Collapsible>
          );
        })}
      </div>
    </section>
  );
}
