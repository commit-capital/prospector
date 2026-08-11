import type { ReactNode } from "react";
import { Link } from "react-router";
import type { PRRow, PRResponses, ChecksRollup, AuthorStats, LocBreakdown } from "../../api";
import { InfoTip, type InstanceWhy } from "../InfoTip";
import { term, safetyEntry, dispositionEntry } from "../../glossary";
import type { GlossaryEntry } from "../../glossary";
import { timeAgo } from "../../timeAgo";
import { GreptileCell } from "./GreptileCell";
import { GitHubPRLink } from "../GitHubPRLink";
import { ConflictChip, DraftChip, TierChip, AckButton } from "../Chips";
import { TRUSTED_AUTHOR_BADGE } from "../TrustedAuthor";
import { LinkedIssues } from "../LinkedIssues";

// Visual aid: a star trails a trusted contributor's handle wherever authors
// are shown (the profile's trusted_authors list, surfaced per-row as trusted_author).

// Extra context a cell may need beyond its row (kept minimal + explicit).
export interface CellCtx {
  deepReasons: Map<number, string>;
  // Mark a PR's response signal as seen (#537); the ack is shared, so it
  // clears the row from the `responses` filter for every operator.
  ackResponse: (pr: number) => Promise<void>;
}

export interface ColumnDef {
  key: string;
  label: string;            // header text + toggle-chip text
  defaultOn: boolean;       // visible when the user has no stored override
  fixed?: boolean;          // always shown, never in the toggle menu (anchors)
  term?: string;            // glossary key → header InfoTip
  numeric?: boolean;        // right-aligned (the `num` class) on header + cell
  stopOpen?: boolean;       // cell swallows the row-open click (links)
  cellClass?: string;       // <td> className (e.g. "mono", "muted small")
  capability?: "review";    // gated on a backend capability — hidden when absent
  cell: (r: PRRow, ctx: CellCtx) => ReactNode;
}

const NOT_ANALYZED: GlossaryEntry = {
  title: "Not analyzed yet",
  meaning: "The pipeline hasn't assigned a disposition to this PR — it hasn't been through ANALYZE, or its analysis went stale and needs a re-run.",
};

// The instance-specific "why this PR landed here" shown under the disposition's
// glossary meaning — pulled straight from the row's analysis.
function dispoWhy(r: PRRow): InstanceWhy | undefined {
  const pa = r.proposed_action;
  if (!pa) return undefined;
  const human = r.disposition === "needs-human";
  return {
    rationale: pa.rationale,
    asks: r.disposition === "request-changes" ? pa.asks ?? null : null,
    stale: pa.fresh === false,
    tone: human ? "danger" : "accent",
    label: human ? "Why a human is needed" : "Why this PR",
  };
}

// chips for how the author responded to our triage since we acted (a render
// helper, not a component — columns.tsx is a data registry, not a component module)
function responseChips(resp: PRResponses): ReactNode {
  const chips = [
    resp.reopened && { k: "reopened", icon: "↩", label: "reopened", tone: "red" },
    resp.new_commits && { k: "new_commits", icon: "⬆", label: "new commits", tone: "blue" },
    resp.replied && { k: "replied", icon: "💬", label: "replied", tone: "gold" },
  ].filter(Boolean) as { k: string; icon: string; label: string; tone: string }[];
  const resubmittedNode = resp.resubmitted && resp.resubmitted_pr ? (
    <GitHubPRLink key="resubmitted" n={resp.resubmitted_pr} className="chip chip-green sm">
      ⤴ resubmitted #{resp.resubmitted_pr}
    </GitHubPRLink>
  ) : resp.resubmitted ? (
    <span key="resubmitted" className="chip chip-green sm">⤴ resubmitted</span>
  ) : null;
  if (!chips.length && !resubmittedNode) return null;
  return (
    <>
      {chips.map((c) => (
        <span key={c.k} className={`chip chip-${c.tone} sm`}> {c.icon} {c.label}</span>
      ))}
      {resubmittedNode}
    </>
  );
}

function fmtDate(iso: string | null | undefined): string {
  return iso ? String(iso).slice(0, 10) : "";
}

function pctOf(rate: number | null | undefined): number | null {
  if (rate == null) return null;
  return Math.round(rate <= 1 ? rate * 100 : rate);
}

// --- hover bodies: structured data, one item per line, no generic blurb ---

function ciBody(c: ChecksRollup): ReactNode {
  return (
    <div className="tip-list">
      {c.checks.map((x, i) => (
        <div key={i} className={`tip-check tip-${x.status}`}>
          <span className="tip-mark">{x.status === "pass" ? "✓" : x.status === "fail" ? "✗" : x.status === "warn" ? "!" : "•"}</span>
          <span className="tip-name">{x.name}</span>
        </div>
      ))}
    </div>
  );
}

function locBody(b: LocBreakdown): ReactNode {
  const cats = Object.entries(b.by_category);
  return (
    <div className="tip-rows">
      <div className="tip-row"><span>Effective</span><b>{b.effective}</b></div>
      <div className="tip-row"><span>Raw</span><b>{b.raw}</b></div>
      {b.artifact > 0 && (
        <div className="tip-row"><span>Generated</span><b>{b.artifact}{b.dominant_artifact ? ` (${b.dominant_artifact})` : ""}</b></div>
      )}
      {cats.length > 0 && (
        <div className="tip-sub">
          {cats.map(([c, v]) => (
            <div key={c} className="tip-row"><span>{c}</span><b className="mono">+{v.additions} −{v.deletions}</b></div>
          ))}
        </div>
      )}
    </div>
  );
}

function authorBody(a: AuthorStats, trusted: boolean): ReactNode {
  const pct = pctOf(a.merge_rate);
  const rows: [string, ReactNode][] = [];
  if (pct != null) rows.push(["Merge rate", `${pct}%`]);
  if (a.total != null) rows.push(["Total PRs", a.total]);
  if (a.merged != null) rows.push(["Merged", a.merged]);
  if (a.open != null) rows.push(["Open", a.open]);
  if (a.closed_unmerged != null) rows.push(["Closed unmerged", a.closed_unmerged]);
  if (a.comments != null) rows.push(["Comments", a.comments]);
  return (
    <div className="tip-rows">
      <div className="tip-head">@{a.handle}{trusted ? ` ${TRUSTED_AUTHOR_BADGE}` : ""}</div>
      {rows.map(([k, v]) => <div key={k} className="tip-row"><span>{k}</span><b>{v}</b></div>)}
    </div>
  );
}

function updatedBody(r: PRRow): ReactNode {
  const resp = r.responses;
  const acted = resp && (resp.reopened || resp.new_commits || resp.replied || resp.resubmitted);
  return (
    <div className="tip-rows">
      {r.created_at && <div className="tip-row"><span>Opened</span><b>{fmtDate(r.created_at)}</b></div>}
      {r.updated_at && <div className="tip-row"><span>Last update</span><b>{fmtDate(r.updated_at)} · {timeAgo(r.updated_at)}</b></div>}
      {acted && resp && (
        <div className="tip-sub">
          <div className="tip-head">Since we acted</div>
          {resp.reopened && <div className="tip-act">↩ reopened</div>}
          {resp.new_commits && <div className="tip-act">⬆ new commits pushed</div>}
          {resp.replied && <div className="tip-act">💬 replied</div>}
          {resp.resubmitted && (
            <div className="tip-act">
              ⤴ resubmitted{resp.resubmitted_pr ? <> as <GitHubPRLink n={resp.resubmitted_pr} /></> : ""}
            </div>
          )}
          {(resp.by || resp.last_response_at) && (
            <div className="tip-meta">{resp.by ? `@${resp.by}` : ""}{resp.by && resp.last_response_at ? " · " : ""}{resp.last_response_at ? fmtDate(resp.last_response_at) : ""}</div>
          )}
          {resp.snippet && <div className="tip-quote">"{resp.snippet}"</div>}
        </div>
      )}
    </div>
  );
}

// Safety verdict + its findings — so the Safety column carries everything the
// (now removed) Findings column showed, plus what the verdict means.
function safetyBody(r: PRRow): ReactNode {
  const entry = safetyEntry(r.safety ?? "not-run");
  const findings = r.safety_titles ?? [];
  return (
    <div className="tip-rows">
      {entry && <div className="tip-head">{entry.title}</div>}
      {entry?.meaning && <div className="glo-pop-meaning">{entry.meaning}</div>}
      {findings.length > 0 && (
        <div className="tip-sub">
          <div className="tip-head">{findings.length} finding{findings.length === 1 ? "" : "s"}</div>
          {findings.map((f, i) => (
            <div key={i} className="tip-act"><b>{f.severity}</b> {f.title}{f.location ? <span className="tip-meta"> · {f.location}</span> : null}</div>
          ))}
        </div>
      )}
    </div>
  );
}

export const COLUMNS: ColumnDef[] = [
  // --- anchors (always shown, not in the toggle menu) + current default-on set ---
  // The PR number links straight to GitHub (new tab); stopOpen keeps that click
  // off the row so clicking anywhere else opens the in-app sidebar (#193).
  { key: "pr", label: "PR", defaultOn: true, fixed: true, term: "col.pr", stopOpen: true, cellClass: "mono",
    cell: (r) => <GitHubPRLink n={r.number} url={r.url} /> },
  { key: "loc", label: "LOC", defaultOn: true, term: "col.loc", numeric: true, cellClass: "num mono small",
    cell: (r) => {
      const b = r.loc_breakdown;
      if (b) {
        const noisy = b.raw > 0 && b.effective / b.raw < 0.5;
        const text = noisy ? `${b.effective} / ${b.raw}` : String(b.effective);
        return (
          <InfoTip body={locBody(b)} cue={false} focusable={false}>
            <span className={noisy ? "loc-noisy" : undefined}>{text}</span>
          </InfoTip>
        );
      }
      const add = r.signals?.additions, del = r.signals?.deletions;
      if (add == null && del == null) return "—";
      return String((add ?? 0) + (del ?? 0));
    } },
  { key: "files", label: "Files", defaultOn: true, term: "col.files", numeric: true, cellClass: "num mono small",
    cell: (r) => r.signals?.changed_files ?? "—" },
  { key: "tier", label: "Tier", defaultOn: true, term: "col.tier",
    cell: (r) => (r.risk_tier == null ? "—" : <TierChip tier={r.risk_tier} />) },
  { key: "title", label: "Title", defaultOn: true, fixed: true,
    cell: (r, ctx) => (
      <>
        <DraftChip draft={r.draft} />{r.draft ? " " : ""}{r.title}
        {r.signals?.conflicts && <> <ConflictChip /></>}
        {ctx.deepReasons.has(r.number) && (
          <InfoTip cue={false} focusable={false}
            entry={{ title: "Why Deep Search matched this", meaning: ctx.deepReasons.get(r.number) || "(the agent gave no reason)" }}>
            <span className="chip chip-blue sm"> 🪄 why</span>
          </InfoTip>
        )}
      </>
    ) },
  { key: "author", label: "Author", defaultOn: true, term: "col.author", cellClass: "muted small",
    cell: (r) => {
      const a = r.author_stats;
      const handle = r.author ?? "—";
      const label = r.trusted_author ? `${handle}\u00A0${TRUSTED_AUTHOR_BADGE}` : handle;
      return a
        ? <InfoTip body={authorBody(a, !!r.trusted_author)} cue={false} focusable={false}><span>{label}</span></InfoTip>
        : <>{label}</>;
    } },
  { key: "updated", label: "Updated", defaultOn: true, term: "col.updated", cellClass: "muted small",
    stopOpen: true,
    cell: (r, ctx) => (
      <InfoTip body={updatedBody(r)} cue={false} focusable={false}>
        <span>
          {timeAgo(r.updated_at)}
          {r.responses ? responseChips(r.responses) : null}
          {r.responses && <AckButton pr={r.number} ack={r.responses.ack} onAck={ctx.ackResponse} />}
        </span>
      </InfoTip>
    ) },
  { key: "cluster", label: "Cluster", defaultOn: true, term: "col.cluster", stopOpen: true, cellClass: "mono",
    cell: (r) => r.clusters.length > 0
      ? <>{r.clusters.map((cid, i) => <span key={cid}>{i > 0 ? ", " : ""}<Link to={`/clusters/${cid}`}>{cid}</Link></span>)}</>
      : "—" },
  { key: "safety", label: "Safety", defaultOn: true, term: "col.safety",
    cell: (r) => (
      <InfoTip body={safetyBody(r)} cue={false} focusable={false}>
        <span>{r.safety === "GREEN" ? "🟢" : r.safety === "YELLOW" ? "🟡" : r.safety === "RED" ? "🔴" : "—"}</span>
      </InfoTip>
    ) },
  { key: "disposition", label: "Disposition", defaultOn: true, term: "col.disposition",
    cell: (r) => (r.out_of_scope
      ? <InfoTip entry={term("deferred")} cue={false} focusable={false}><span className="chip chip-muted sm">deferred</span></InfoTip>
      : r.disposition
        ? <InfoTip entry={dispositionEntry(r.disposition)} why={dispoWhy(r)} cue={false} focusable={false}>{r.disposition}</InfoTip>
        : <InfoTip entry={NOT_ANALYZED} cue={false} focusable={false}>—</InfoTip>) },

  // --- default-off opt-in columns ---
  { key: "greptile", label: "Greptile", defaultOn: false, term: "col.greptile", capability: "review",
    cell: (r) => <GreptileCell n={r.number} score={r.signals?.greptile ?? null} stale={r.signals?.greptile_stale ?? null}
      severity={r.signals?.greptile_severity ?? null} /> },
  { key: "checks", label: "CI checks", defaultOn: false, term: "col.checks",
    cell: (r) => {
      const c = r.checks;
      if (!c || c.total === 0) return "—";
      const failed = c.checks.some((x) => x.status === "fail");
      return (
        <InfoTip body={ciBody(c)} cue={false} focusable={false}>
          <span className={`chip ${failed ? "chip-red" : "chip-green"} sm`}>{c.passed}/{c.total}</span>
        </InfoTip>
      );
    } },
  { key: "drift", label: "Drift", defaultOn: false, term: "filter.drift",
    cell: (r) => {
      const st = r.drift_state;
      if (!st) return "—";
      return (
        <InfoTip entry={term(`drift.${st}`)} cue={false} focusable={false}>
          <span className="chip chip-muted sm">{st}</span>
        </InfoTip>
      );
    } },
  { key: "merge", label: "Merge-ready", defaultOn: false, term: "col.merge",
    cell: (r) => {
      const g = r.merge_gate;
      if (!g) return "—";
      return (
        <InfoTip entry={term("col.merge")} why={g.reason ? { rationale: g.reason, label: g.ok ? "Mergeable" : "Blocked" } : null} cue={false} focusable={false}>
          <span className={`chip ${g.ok ? "chip-green" : "chip-muted"} sm`}>{g.ok ? "✓ ready" : "✗ blocked"}</span>
        </InfoTip>
      );
    } },
  { key: "age", label: "Age", defaultOn: false, term: "col.age", cellClass: "muted small",
    cell: (r) => (r.age_days != null ? `${r.age_days}d` : "—") },
  { key: "author_rate", label: "Author rate", defaultOn: true, term: "col.author_rate",
    cell: (r) => {
      const a = r.author_stats;
      const pct = pctOf(a?.merge_rate);
      if (pct == null) return "—";
      return (
        <InfoTip body={a ? authorBody(a, !!r.trusted_author) : undefined} cue={false} focusable={false}><span>{pct}%</span></InfoTip>
      );
    } },
  { key: "summary", label: "Agent Summary", defaultOn: false, term: "col.summary",
    cell: (r) => {
      const s = r.summary;
      if (!s?.one_liner) return "—";
      return (
        <InfoTip entry={{ title: "Agent summary", meaning: s.one_liner }}
          why={s.primary_change ? { rationale: s.primary_change, label: "Primary change" } : null} cue={false} focusable={false}>
          <span className="col-summary">{s.one_liner}</span>
        </InfoTip>
      );
    } },
  { key: "pain", label: "Pain", defaultOn: false, numeric: true, cellClass: "num mono small",
    cell: (r) => {
      const score = r.pain_score;
      const b = r.pain_breakdown;
      if (score == null || score === 0) return "—";
      const tipBody = b ? (
        <div className="tip-rows">
          <div className="tip-head">Community Pain Score</div>
          <div className="tip-row"><span>Issue pain</span><b>{b.issue_pain.toFixed(2)} ({b.linked_issues} issue{b.linked_issues === 1 ? "" : "s"})</b></div>
          <div className="tip-row"><span>PR comments</span><b>{b.pr_comments}</b></div>
          <div className="tip-row"><span>PR reactions</span><b>{b.pr_reactions}</b></div>
        </div>
      ) : undefined;
      return (
        <InfoTip body={tipBody} entry={tipBody ? undefined : { title: "Community Pain Score", meaning: "Linked-issue pain + PR engagement" }} cue={false} focusable={false}>
          <span>{score.toFixed(2)}</span>
        </InfoTip>
      );
    } },
  { key: "issues", label: "Linked Issues", defaultOn: true, term: "col.issues", stopOpen: true,
    cell: (r) => <LinkedIssues issues={r.issues} limit={6} /> },
];
