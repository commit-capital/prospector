import type { ReactNode } from "react";
import type { ChecksRollup, CheckItem } from "../api";
import { localDateTime, timeAgo } from "../timeAgo";
import { Collapsible } from "./Collapsible";

const ICON = { pass: "✓", warn: "⚠", fail: "✗", na: "–" } as const;
const TONE: Record<CheckItem["status"], "green" | "yellow" | "red" | "muted"> = {
  pass: "green", warn: "yellow", fail: "red", na: "muted",
};

function ageTitle(chk: CheckItem): string {
  if (!chk.at) return "no timestamp recorded";
  const against = chk.against ? ` · against ${chk.against.slice(0, 7)}` : "";
  return `checked ${localDateTime(chk.at)}${against}`;
}

function CheckRowLine({ chk, action, next }: { chk: CheckItem; action?: ReactNode; next?: boolean }) {
  return (
    <span className="check-row-line">
      <span className={`check-ico st-${chk.status}`} aria-hidden="true">{next ? "→" : ICON[chk.status]}</span>
      <span className="check-name">{chk.name}</span>
      {next && <span className="chip chip-blue sm">Next step</span>}
      <span className="check-detail muted small">{chk.detail}</span>
      <span className={`check-age small ${chk.stale ? "check-age-stale" : "muted"}`} title={ageTitle(chk)}>
        {chk.at ? timeAgo(chk.at) : "—"}
      </span>
      {action && <span className="check-action">{action}</span>}
    </span>
  );
}

function CheckRow({ chk, action, body, next }: {
  chk: CheckItem; action?: ReactNode; body?: ReactNode; next?: boolean;
}) {
  if (!body) {
    return (
      <div className={`check-row check-${chk.status}`}>
        <CheckRowLine chk={chk} action={action} next={next} />
      </div>
    );
  }
  // The summary renders inside the header <button>, so the action —
  // itself a button — goes in the `action` slot, a sibling of it
  // (HTML forbids a button inside a button).
  return (
    <Collapsible className={`check-row-collapsible check-${chk.status}`}
      tone={TONE[chk.status]} summary={<CheckRowLine chk={chk} next={next} />} action={action}>
      {body}
    </Collapsible>
  );
}

/** #550/#581: the topline surface — the processing stages (summarized,
 *  clustered, analyzed) and then every check this deployment could run on this
 *  PR, each with its status, the commit it describes, the action that unblocks
 *  it right next to the status, and — when there's more to say than the
 *  one-line summary carries — an expandable toggle for the detail. The first
 *  stage still owed is marked as the next step; the checks below it stay live. */
export function ChecksPanel({ c, stages, headSha, actions, bodies }: {
  c: ChecksRollup | undefined;
  stages?: CheckItem[];
  /** the PR's head as last seen — what a stale row is older than */
  headSha?: string | null;
  /** per-key control(s) rendered inline with that row (e.g. "↻ Re-run"). */
  actions?: Partial<Record<string, ReactNode>>;
  /** per-key expanded detail; a row with no body isn't expandable. */
  bodies?: Partial<Record<string, ReactNode>>;
}) {
  if (!c || c.checks.length === 0) return null;
  const ratio = c.total > 0 ? c.passed / c.total : null;
  const tone = ratio == null ? "muted" : ratio === 1 ? "green" : ratio >= 0.6 ? "amber" : "red";
  const steps = stages ?? [];
  const next = steps.find((s) => s.status !== "pass")?.key;
  const anyStale = [...steps, ...c.checks].some((r) => r.stale);
  const row = (chk: CheckItem) => (
    <CheckRow key={chk.key} chk={chk} action={actions?.[chk.key]} body={bodies?.[chk.key]}
      next={chk.key === next} />
  );
  return (
    <section className={`checks-panel checks-panel-${tone}`}>
      <div className="checks-panel-head">
        <h3>Checks</h3>
        <span className={`chip chip-${tone === "amber" ? "amber" : tone} sm`}>
          {ratio == null ? "none run yet" : `${c.passed}/${c.total} passed`}
        </span>
      </div>
      {anyStale && (
        <div className="checks-stale-note small" role="status">
          ⚠ This PR is now at <code>{headSha?.slice(0, 7) ?? "a newer commit"}</code> — rows marked
          “older commit” describe an earlier commit until they're redone.
        </div>
      )}
      <div className="checks-panel-grid">
        {steps.map(row)}
        {steps.length > 0 && <div className="checks-divider" role="separator" />}
        {c.checks.map(row)}
      </div>
    </section>
  );
}
