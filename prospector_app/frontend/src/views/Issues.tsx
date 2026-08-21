import { Fragment, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { api, type IssueRow, type IssueDupGroup, type IssuePR, type IssueExecResult, type IssueDisposition, type IssueTriageDisposition, type IssueFilterSpec, type IssueLikelyFixedItem } from "../api";
import { PRLink } from "../components/PRLink";
import { GitHubPRLink } from "../components/GitHubPRLink";
import { SuggestedActions } from "../components/SuggestedActions";
import { useExec } from "../ExecContext";
import { useIssueFlyout } from "../useIssueFlyout";
import { useRepoMeta } from "../RepoMetaContext";
import { stopRowOpen } from "../rowOpen";
import { cycleSort, type SortDir } from "../sortCycle";
import { FilterSummary } from "../components/shared/FilterSummary";
import { buildIssueFilterParts } from "../components/issues/issueFilterParts";
import { IssueColumnFilterPopout, ISSUE_FILTERABLE_COLS, isIssueColFilterActive } from "../components/issues/IssueColumnFilterPopout";
import { TrustedAuthorName } from "../components/TrustedAuthor";
import { AuthorHover } from "../components/AuthorHover";

const PAGE_SIZE = 50;
type IssueSortKey = "number" | "title" | "author" | "pain" | "repro" | "dups" | "prs" | "disposition" | "subsystem";
const ISSUE_DESC_FIRST = new Set<IssueSortKey>(["number", "pain", "repro", "dups", "prs"]);

// A GitHub issue number → its issue on github.com. Row-level clicks open the
// in-app detail flyout, so this link stops the row handler.
function IssueLink({ n, children }: { n: number; children?: React.ReactNode }) {
  const { issueUrl } = useRepoMeta();
  return (
    <a href={issueUrl(n)} target="_blank" rel="noreferrer"
       className="gh-pr-link" title="Open this issue on GitHub ↗"
       onClick={stopRowOpen}>{children ?? `#${n}`}</a>
  );
}

const DISP_CHIP: Record<IssueTriageDisposition, { cls: string; hint: string }> = {
  "link-pr": { cls: "chip-green", hint: "An open PR already addresses this issue — see its linked PRs" },
  "close-dup": { cls: "chip-purple", hint: "Duplicate of its cluster's canonical issue" },
  "close-fixed": { cls: "chip-blue", hint: "Already fixed by a merged PR — safe to close as fixed" },
  "request-repro": { cls: "chip-yellow", hint: "Real but under-specified — needs reporter info (see asks)" },
  "needs-human": { cls: "chip-muted", hint: "Ambiguous, a judgement call, or a feature request" },
};

// The issue pipeline's triage verdict as a chip; unanalyzed issues render a dash.
export function DispositionChip({ d }: { d: IssueTriageDisposition | null }) {
  if (!d) return <span className="muted">—</span>;
  const { cls, hint } = DISP_CHIP[d];
  return <span className={`chip ${cls} sm`} title={hint}>{d}</span>;
}

// The PRs that may address an issue. Evidence-backed matches — an explicit
// Fixes/Closes/Resolves in the PR body, a merged fixer the already-fixed
// detector attributed by symptom (fix-found), or a PR the issue's own text names
// (issue-ref) — render as links, strongest evidence first and most-resolved first
// (merged, then closed, then open); a PR that merely shares the issue's subsystem
// tag is weak evidence and collapses into a muted count. A PR in the app's
// store — open, merged, or closed — opens in the in-app flyout (pr-ref); one that
// isn't in the store links out to GitHub, with a state chip (purple merged /
// muted closed) when its state is known.
const EVIDENCE: Record<string, string> = {
  explicit: "explicit Fixes/Closes/Resolves reference in the PR body",
  "fix-found": "merged fix attributed to this issue by the already-fixed detector",
  "issue-ref": "referenced from the issue's own text",
};

export function LinkedPRs({ prs, count, referencedCount }: { prs: IssuePR[]; count?: number; referencedCount?: number }) {
  const referenced = prs.filter((p) => (p.how ?? "") in EVIDENCE);
  const total = count ?? prs.length;
  const nReferenced = referencedCount ?? referenced.length;
  const nSubsystem = total - nReferenced;
  if (!total) return <span className="muted">—</span>;
  return (
    <span className="issue-prs">
      {referenced.slice(0, 6).map((p, i) => {
        const resolved = p.state === "merged" || p.state === "closed";
        const evidence = EVIDENCE[p.how ?? ""];
        return (
          <span key={p.pr} title={p.title ? `${p.title} — ${evidence}` : evidence}>{i > 0 && " "}
            {p.in_store
              ? <PRLink n={p.pr} className="pr-ref" />
              : <GitHubPRLink n={p.pr} className="pr-ref" />}
            {resolved && <span className={`chip sm ${p.state === "merged" ? "chip-purple" : "chip-muted"}`}
                               title="Current state on GitHub">{p.state}</span>}
          </span>
        );
      })}
      {nReferenced > 6 && <span className="muted small"> +{nReferenced - 6}</span>}
      {nSubsystem > 0 && (
        <span className="muted small"
              title="PRs that only share the issue's subsystem tag — no reference either way">
          {nReferenced > 0 ? ` +${nSubsystem} same-subsystem` : `${nSubsystem} same-subsystem`}
        </span>
      )}
    </span>
  );
}

export function ReproChip({ grade }: { grade: string | null }) {
  if (!grade) return <span className="muted">—</span>;
  const cls = grade <= "B" ? "chip-green" : grade <= "D" ? "chip-yellow" : "chip-muted";
  return <span className={`chip ${cls} sm`} title="Reproduction quality grade from issue triage (A best → F worst)">repro {grade}</span>;
}

// One duplicate-cluster card: the canonical issue, the dups to close against it,
// and the PRs cross-linked to the cluster. Closing posts the dup-pointer comment
// + closes each dup as the configured bot (gated; dry-run unless a bot token exists).
function DupGroupCard({ g }: { g: IssueDupGroup }) {
  const { botLogin, dryRun } = useExec();
  const [results, setResults] = useState<Record<number, IssueExecResult>>({});
  const [busy, setBusy] = useState(false);
  const [excluded, setExcluded] = useState<Set<number>>(new Set());
  const [copied, setCopied] = useState(false);
  const [mode, setMode] = useState<"dup" | "fixed">("dup");
  const [menuOpen, setMenuOpen] = useState(false);
  // Prefill the box with the exact default note the executor would post for the
  // current mode, editable in place. Clearing the box falls back to the server
  // template at post time.
  const [comment, setComment] = useState(g.dup_comment);
  // Switch close mode and reset the box to that mode's default note.
  const pickMode = (m: "dup" | "fixed") => {
    setMode(m);
    setComment(m === "fixed" ? (g.fixed_comment ?? "") : g.dup_comment);
    setMenuOpen(false);
  };
  const [sp, setSp] = useSearchParams();
  const ref = useRef<HTMLElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // The merged PR that explicitly references an issue in this cluster, if any —
  // enables "close as fixed". Only an explicit Fixes/Closes reference qualifies:
  // a subsystem tag-match or a PR merely named in the issue's text (which can be
  // anti-evidence — "still broken despite PR #N") is never offered as the fixer.
  const fixer = g.linked_prs.find((p) => p.how === "explicit" && p.state === "merged")?.pr ?? null;

  // Dismiss the close-mode menu on a click outside it.
  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [menuOpen]);

  // A shareable deep-link to this card: ?dup=<cluster> selects it; landing on that
  // URL scrolls the card into view and highlights it.
  const linked = g.cluster != null && sp.get("dup") === String(g.cluster);
  useEffect(() => {
    // Instant scroll — Chrome silently drops repeated window-level smooth
    // scrollIntoView calls, and landing on the card is the point of the link.
    if (linked) ref.current?.scrollIntoView({ block: "center" });
  }, [linked]);
  const copyLink = () => {
    if (g.cluster == null) return;
    setSp((prev) => { prev.set("dup", String(g.cluster)); return prev; }, { replace: true });
    const url = `${window.location.origin}${window.location.pathname}?dup=${g.cluster}`;
    void navigator.clipboard?.writeText(url);
    setCopied(true);
    setTimeout(() => setCopied(false), 1400);
  };

  const dupTargets = g.dups.filter((d) => !excluded.has(d.number)).map((d) => d.number);
  // Close-as-fixed also closes the canonical (it too is fixed by the merged PR);
  // close-as-dup keeps the canonical as the survivor.
  const targets = mode === "fixed" ? [g.canonical, ...dupTargets] : dupTargets;
  const closeOne = async (n: number) => {
    const c = comment.trim() || undefined;
    const res = mode === "fixed" && fixer != null
      ? await api.closeIssueFixed(n, fixer, dryRun, c)
      : await api.closeIssueDup(n, g.canonical, dryRun, c);
    setResults((r) => ({ ...r, [n]: res }));
  };
  const closeAll = async () => {
    setBusy(true);
    setMenuOpen(false);
    for (const n of targets) await closeOne(n);
    setBusy(false);
  };
  const toggle = (n: number) =>
    setExcluded((s) => { const next = new Set(s); if (next.has(n)) next.delete(n); else next.add(n); return next; });

  return (
    <section ref={ref} id={g.cluster != null ? `dup-${g.cluster}` : undefined}
             className={`dup-card${linked ? " dup-card-linked" : ""}`}>
      <div className="dup-card-head">
        <div>
          <span className={`dup-keep${mode === "fixed" ? " dup-keep-off" : ""}`}
                title={mode === "fixed" ? "Closed as fixed too — it is resolved by the merged PR" : "Kept as the canonical survivor"}>
            {mode === "fixed" ? "CLOSE" : "KEEP"}</span> <IssueLink n={g.canonical} /> <b className="dup-canon-title">{g.canonical_title}</b>
          {results[g.canonical] && <span className={`chip chip-${results[g.canonical].status === "executed" ? "green" : results[g.canonical].status === "error" ? "red" : "muted"} sm`} title={results[g.canonical].detail}>{results[g.canonical].status}</span>}
          {g.cluster != null && (
            <button className="dup-permalink" onClick={copyLink}
                    title="Copy a shareable link to this cluster">{copied ? "copied ✓" : "🔗"}</button>
          )}
        </div>
        <div className="dup-card-meta">
          {g.pain != null && <span className="chip chip-purple sm" title="Pain rank for this cluster (higher = more impactful)">pain {g.pain.toFixed(2)}</span>}
          {g.subsystem && <span className="chip chip-muted sm">{g.subsystem}</span>}
        </div>
      </div>
      {g.label && <div className="muted small dup-label">{g.label}</div>}
      {g.linked_prs.length > 0 && (
        <div className="dup-prs"><span className="muted small">PRs that may fix it: </span><LinkedPRs prs={g.linked_prs} /></div>
      )}

      <table className="grid compact dup-table">
        <thead><tr><th></th><th>Duplicate</th><th>Author</th><th>Repro</th><th></th></tr></thead>
        <tbody>
          {g.dups.map((d) => {
            const res = results[d.number];
            const ex = excluded.has(d.number);
            return (
              <tr key={d.number} className={ex ? "row-skipped" : ""}>
                <td><input type="checkbox" checked={!ex} onChange={() => toggle(d.number)} title="Include in the close batch" /></td>
                <td className="mono"><IssueLink n={d.number} /> <span className="dup-dup-title">{d.title}</span></td>
                <td className="muted small"><TrustedAuthorName author={d.author} trusted={d.trusted_author} fallback="" /></td>
                <td><ReproChip grade={d.repro_grade} /></td>
                <td>
                  {res
                    ? <span className={`chip chip-${res.status === "executed" ? "green" : res.status === "error" ? "red" : "muted"}`} title={res.detail}>{res.status}</span>
                    : <button className="link-btn" disabled={ex || busy} onClick={() => closeOne(d.number)}>close as {mode === "fixed" ? "fixed" : "dup"}</button>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <textarea className="dup-comment" rows={2} value={comment} onChange={(e) => setComment(e.target.value)}
        placeholder={mode === "fixed"
          ? `Cleared — the default “fixed by #${fixer}” note will be posted…`
          : `Cleared — the default “duplicate of #${g.canonical}” note will be posted…`} />

      <div className="dup-card-actions">
        <div className="split-btn" ref={menuRef}>
          <button className={`btn-primary sm ${!dryRun ? "btn-live" : ""}`} disabled={busy || targets.length === 0} onClick={closeAll}>
            {busy ? "Working…"
              : mode === "fixed"
                ? `${dryRun ? "" : "● "}Close ${targets.length} as fixed by #${fixer}${dryRun ? " (dry-run)" : ` as ${botLogin}`}`
                : `${dryRun ? "" : "● "}Close ${targets.length} duplicate${targets.length === 1 ? "" : "s"}${dryRun ? " (dry-run)" : ` as ${botLogin}`}`}
          </button>
          {fixer != null && (
            <>
              <button className={`btn-primary sm split-btn-caret ${!dryRun ? "btn-live" : ""}`} disabled={busy}
                      title="Choose how to close" onClick={() => setMenuOpen((o) => !o)}>▾</button>
              {menuOpen && (
                <div className="split-menu">
                  <button className={mode === "dup" ? "on" : ""} onClick={() => pickMode("dup")}>
                    Close as dup of #{g.canonical}</button>
                  <button className={mode === "fixed" ? "on" : ""} onClick={() => pickMode("fixed")}>
                    Close as fixed by #{fixer}</button>
                </div>
              )}
            </>
          )}
        </div>
        <span className="muted small">{mode === "fixed"
          ? `posts a “fixed by #${fixer}” comment + closes #${g.canonical} and ${dupTargets.length} dup${dupTargets.length === 1 ? "" : "s"} as completed — reversible`
          : `posts the “duplicate of #${g.canonical}” comment + closes each — reversible`}</span>
      </div>
    </section>
  );
}

// The likely-fixed review list: the fix scan sees a probable fix but without a
// confirmed fixer, so each row is a judgement call — the issue link opens
// GitHub, and closing happens from the issue flyout's action bar.
function LikelyFixedSection({ items }: { items: IssueLikelyFixedItem[] }) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  if (items.length === 0) {
    return <div className="callout">No likely-fixed issues to review.</div>;
  }
  return (
    <table className="grid compact fixed-table">
      <thead><tr><th>#</th><th>Issue</th><th>Pain</th></tr></thead>
      <tbody>
        {items.map((it) => (
          <Fragment key={it.number}>
            <tr>
              <td className="mono"><IssueLink n={it.number} /></td>
              <td>
                <button className="link-btn" onClick={() => setExpanded((s) => {
                  const next = new Set(s);
                  if (next.has(it.number)) next.delete(it.number); else next.add(it.number);
                  return next;
                })} title={expanded.has(it.number) ? "Hide the fix scan's reasoning" : "Show the fix scan's reasoning"}>
                  {expanded.has(it.number) ? "▾" : "▸"}</button>{" "}
                {it.title}
              </td>
              <td className="mono small">{it.pain != null ? it.pain.toFixed(2) : "—"}</td>
            </tr>
            {expanded.has(it.number) && (
              <tr className="fixed-row-detail">
                <td></td>
                <td colSpan={2}>
                  {it.gist && <div style={{ whiteSpace: "pre-wrap" }}>{it.gist}</div>}
                  {it.rationale && <div className="muted small" style={{ whiteSpace: "pre-wrap", marginTop: 6 }}>{it.rationale}</div>}
                </td>
              </tr>
            )}
          </Fragment>
        ))}
      </tbody>
    </table>
  );
}

type Disposition = IssueDisposition;
const DISPOSITIONS: { key: Disposition; label: string }[] = [
  { key: "not-planned", label: "not planned" },
  { key: "completed", label: "completed" },
  { key: "fixed", label: "fixed" },
  { key: "dup", label: "duplicate" },
];

// The bulk close bar under the All-issues table: one shared disposition + comment,
// closing every checked issue as the configured bot (gated, reversible, logged). The two
// plain reasons require a comment; "fixed" needs a fixer PR # and "duplicate" a
// canonical issue # (comment optional — a templated note is posted when empty), and
// those attribute to the fixed/dup activity cards. The chosen PR/issue # applies to
// the whole selection. Dry-run by default.
function IssueCloseBar({
  selected, onResult, onDone,
}: {
  selected: number[];
  onResult: (n: number, res: IssueExecResult) => void;
  onDone: () => void;
}) {
  const { botLogin, dryRun } = useExec();
  const [disposition, setDisposition] = useState<Disposition>("not-planned");
  const [comment, setComment] = useState("");
  const [fixedBy, setFixedBy] = useState("");
  const [canonical, setCanonical] = useState("");
  const [busy, setBusy] = useState(false);

  const fixerN = Number(fixedBy);
  const canonN = Number(canonical);
  const needsComment = disposition === "not-planned" || disposition === "completed";
  const refOk = disposition === "fixed" ? fixerN > 0
    : disposition === "dup" ? canonN > 0
    : true;
  const canClose = !busy && selected.length > 0 && refOk && (!needsComment || comment.trim().length > 0);

  const closeAll = async () => {
    setBusy(true);
    const body = {
      disposition,
      comment: comment.trim(),
      ...(disposition === "fixed" ? { fixed_by: fixerN } : {}),
      ...(disposition === "dup" ? { canonical: canonN } : {}),
    };
    try {
      for (const n of selected) {
        try {
          const res = await api.closeIssue(n, body, dryRun);
          onResult(n, res);
        } catch (e) {
          onResult(n, { issue: n, action: "CLOSE_ISSUE", status: "error", detail: String(e) });
        }
      }
    } finally {
      setBusy(false);
      onDone();
    }
  };

  return (
    <div className="issue-close-bar">
      <div className="issue-close-bar-head">
        <b>{selected.length}</b> issue{selected.length === 1 ? "" : "s"} selected
        <div className="segmented">
          {DISPOSITIONS.map((d) => (
            <button key={d.key} className={disposition === d.key ? "on" : ""}
              onClick={() => setDisposition(d.key)}>{d.label}</button>
          ))}
        </div>
        {disposition === "fixed" && (
          <input className="search sm issue-ref-input" type="number" min={1} value={fixedBy}
            onChange={(e) => setFixedBy(e.target.value)} placeholder="fixed by PR #" />
        )}
        {disposition === "dup" && (
          <input className="search sm issue-ref-input" type="number" min={1} value={canonical}
            onChange={(e) => setCanonical(e.target.value)} placeholder="duplicate of issue #" />
        )}
      </div>
      <textarea className="dup-comment" rows={2} value={comment} onChange={(e) => setComment(e.target.value)}
        placeholder={needsComment
          ? "Comment posted to each issue before it closes (required)…"
          : disposition === "fixed"
            ? `Optional — defaults to a “fixed by #${fixedBy || "…"}” note…`
            : `Optional — defaults to a “duplicate of #${canonical || "…"}” note…`} />
      <div className="dup-card-actions">
        <button className={`btn-primary sm ${!dryRun ? "btn-live" : ""}`} disabled={!canClose} onClick={closeAll}>
          {busy ? "Working…"
            : `${dryRun ? "" : "● "}Close ${selected.length} issue${selected.length === 1 ? "" : "s"}${dryRun ? " (dry-run)" : ` as ${botLogin}`}`}
        </button>
        <span className="muted small">
          {disposition === "fixed" ? `closes “completed”, fixed by #${fixedBy || "…"} — reversible`
            : disposition === "dup" ? `closes “duplicate” of #${canonical || "…"} — reversible`
            : `closes “${disposition === "completed" ? "completed" : "not planned"}” — reversible`}
        </span>
      </div>
    </div>
  );
}

const DISP_FILTERS: { key: string; label: string }[] = [
  { key: "", label: "all" },
  { key: "link-pr", label: "link-pr" },
  { key: "close-dup", label: "close-dup" },
  { key: "close-fixed", label: "close-fixed" },
  { key: "request-repro", label: "request-repro" },
  { key: "needs-human", label: "needs-human" },
  { key: "none", label: "unanalyzed" },
];

const STATE_FILTERS: { key: string; label: string }[] = [
  { key: "open", label: "open" },
  { key: "closed", label: "closed" },
  { key: "all", label: "all" },
];

function AllIssuesTable({
  rows, total, page, q, sortKey, sortDir, loading, dispFilter, stateFilter,
  onQ, onPage, onSort, onDispFilter, onStateFilter, filterSpec, onFilterSpecChange,
  selected, results, onToggle, onToggleAll,
}: {
  rows: IssueRow[];
  total: number;
  page: number;
  q: string;
  sortKey: IssueSortKey | "";
  sortDir: SortDir | "";
  loading: boolean;
  dispFilter: string;
  stateFilter: string;
  onQ: (q: string) => void;
  onPage: (page: number) => void;
  onSort: (key: IssueSortKey) => void;
  onDispFilter: (d: string) => void;
  onStateFilter: (s: string) => void;
  filterSpec: IssueFilterSpec;
  onFilterSpecChange: (next: IssueFilterSpec) => void;
  selected: Set<number>;
  results: Record<number, IssueExecResult>;
  onToggle: (n: number) => void;
  onToggleAll: (nums: number[], checked: boolean) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const start = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const end = Math.min(total, page * PAGE_SIZE);
  // Which column's filter popout is open, and where to anchor it — mirrors
  // the PR Explorer's per-column filter flyout (#494).
  const [openFilter, setOpenFilter] = useState<{ key: string; rect: DOMRect } | null>(null);
  const thProps = (key: IssueSortKey) => ({
    className: `sortable-th ${sortKey === key ? "sorted" : ""}`,
    onClick: () => onSort(key),
    role: "button" as const,
    "aria-sort": (sortKey === key ? (sortDir === "asc" ? "ascending" : "descending") : "none") as
      "ascending" | "descending" | "none",
    title: "Click to sort by this column",
  });
  const indicator = (key: IssueSortKey) => sortKey === key ? (sortDir === "asc" ? " ▲" : " ▼") : "";
  const filterBtn = (key: string) => {
    if (!ISSUE_FILTERABLE_COLS.has(key)) return null;
    const active = isIssueColFilterActive(key, filterSpec);
    return (
      <button
        className={`col-filter-btn${active ? " col-filter-active" : ""}`}
        title={`Filter by this column${active ? " (active)" : ""}`}
        onClick={(e) => {
          e.stopPropagation();
          const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
          setOpenFilter(openFilter?.key === key ? null : { key, rect });
        }}
      >▾</button>
    );
  };
  const { openIssue } = useIssueFlyout();
  const rowClick = (n: number) => (e: React.MouseEvent) => {
    if (e.shiftKey) return;
    if (e.metaKey || e.ctrlKey) { e.preventDefault(); onToggle(n); return; }
    openIssue(n);
  };
  const filterParts = buildIssueFilterParts(filterSpec);

  return (
    <>
      <div className="table-toolbar">
        <input className="search" placeholder="Search issues…" value={q} onChange={(e) => onQ(e.target.value)} />
        <div className="segmented" title="Filter by GitHub open/closed state">
          {STATE_FILTERS.map((f) => (
            <button key={f.key} className={stateFilter === f.key ? "on" : ""}
              onClick={() => onStateFilter(f.key)}>{f.label}</button>
          ))}
        </div>
        <div className="segmented" title="Filter by the issue pipeline's triage disposition">
          {DISP_FILTERS.map((f) => (
            <button key={f.key} className={dispFilter === f.key ? "on" : ""}
              onClick={() => onDispFilter(f.key)}>{f.label}</button>
          ))}
        </div>
        <input className="author-in" placeholder="label contains" value={filterSpec.labels ?? ""}
          title="Filter by GitHub label (substring match)"
          onChange={(e) => onFilterSpecChange({ ...filterSpec, labels: e.target.value || undefined })} />
        {filterParts.length > 0 && (
          <button className="link-btn" onClick={() => onFilterSpecChange({})}>Clear filters</button>
        )}
        <span className="muted small">
          {loading ? "Loading…" : total === 0 ? "No issues" : `${start}-${end} of ${total}`}
        </span>
        <button className="btn-secondary sm" disabled={page <= 1 || loading} onClick={() => onPage(page - 1)}>Prev</button>
        <button className="btn-secondary sm" disabled={page >= pages || loading} onClick={() => onPage(page + 1)}>Next</button>
      </div>
      <FilterSummary parts={filterParts} total={total} unit="issue" />
      {openFilter && (
        <IssueColumnFilterPopout
          colKey={openFilter.key}
          spec={filterSpec}
          onChange={onFilterSpecChange}
          rect={openFilter.rect}
          onClose={() => setOpenFilter(null)}
        />
      )}
      <table className="grid sortable issues-table">
        <thead><tr>
          <th className="chk-col">
            <input type="checkbox"
              checked={rows.length > 0 && rows.every((r) => selected.has(r.number))}
              ref={(el) => { if (el) el.indeterminate = rows.some((r) => selected.has(r.number)) && !rows.every((r) => selected.has(r.number)); }}
              onChange={(e) => onToggleAll(rows.map((r) => r.number), e.target.checked)}
              title="Select all issues on this page" />
          </th>
          <th {...thProps("number")}>#{indicator("number")}</th>
          <th {...thProps("title")}>Title{indicator("title")}</th>
          <th {...thProps("author")}><span className="th-inner"><span className="th-label">Author{indicator("author")}</span>{filterBtn("author")}</span></th>
          <th {...thProps("pain")}><span className="th-inner"><span className="th-label">Pain{indicator("pain")}</span>{filterBtn("pain")}</span></th>
          <th {...thProps("repro")}><span className="th-inner"><span className="th-label">Repro{indicator("repro")}</span>{filterBtn("repro")}</span></th>
          <th {...thProps("disposition")}
              title="The issue pipeline's triage verdict; sorts most-actionable first, unanalyzed last">
            Disposition{indicator("disposition")}</th>
          <th {...thProps("dups")}><span className="th-inner"><span className="th-label">Dups{indicator("dups")}</span>{filterBtn("dups")}</span></th>
          <th {...thProps("prs")}
              title="Sorts by fix evidence: merged referenced fixers first (Fixes/Closes references and PRs named in the issue text), then referenced PRs, then total linked PRs">
            <span className="th-inner"><span className="th-label">Linked PRs{indicator("prs")}</span>{filterBtn("prs")}</span></th>
          <th {...thProps("subsystem")}><span className="th-inner"><span className="th-label">Subsystem{indicator("subsystem")}</span>{filterBtn("subsystem")}</span></th>
        </tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.number} onClick={rowClick(r.number)}
                className={`rowlink ${selected.has(r.number) ? "row-selected" : ""}`}>
              <td className="chk-col" onClick={stopRowOpen}>
                <input type="checkbox" checked={selected.has(r.number)}
                  onChange={() => onToggle(r.number)} title="Select this issue" />
              </td>
              <td className="mono">
                <IssueLink n={r.number} />
                {r.state === "closed" && (
                  <span className="chip sm chip-muted" title="Closed on GitHub">✓ closed</span>
                )}
                {results[r.number] && (
                  <span className={`chip chip-${results[r.number].status === "executed" ? "green"
                    : results[r.number].status === "error" ? "red" : "muted"} sm`}
                    title={results[r.number].detail}>{results[r.number].status}</span>
                )}
              </td>
              <td>{r.title}{r.is_dup && r.canonical != null && <span className="muted small" title={`Duplicate of #${r.canonical}`}> · dup of <IssueLink n={r.canonical} /></span>}</td>
              <td className="muted small"><AuthorHover author={r.author} trusted={r.trusted_author} stats={r.author_stats} fallback="" /></td>
              <td className="mono small">{r.pain != null ? r.pain.toFixed(2) : "—"}</td>
              <td><ReproChip grade={r.repro_grade} /></td>
              <td><DispositionChip d={r.disposition} /></td>
              <td className="mono small">{r.duplicates.length || "—"}</td>
              <td onClick={stopRowOpen}><LinkedPRs prs={r.linked_prs} count={r.linked_pr_count} referencedCount={r.referenced_pr_count} /></td>
              <td className="muted small">{r.subsystem ?? "—"}</td>
            </tr>
          ))}
          {!loading && rows.length === 0 && (
            <tr><td colSpan={10} className="muted">No matching issues.</td></tr>
          )}
        </tbody>
      </table>
    </>
  );
}

export default function Issues() {
  const { botLogin } = useExec();
  const { meta: repoMeta } = useRepoMeta();
  // ?dup=<cluster> deep-links to a duplicate-triage card: any load or in-app
  // navigation carrying that param lands on the Close-out tab (where the card
  // scrolls into view).
  const [sp] = useSearchParams();
  const dupLinked = sp.get("dup") != null;
  const [rows, setRows] = useState<IssueRow[]>([]);
  const [groups, setGroups] = useState<IssueDupGroup[]>([]);
  const [likelyItems, setLikelyItems] = useState<IssueLikelyFixedItem[]>([]);
  const [total, setTotal] = useState(0);
  const [tab, setTab] = useState<"dups" | "all">(dupLinked ? "dups" : "all");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [sortKey, setSortKey] = useState<IssueSortKey | "">("pain");
  const [sortDir, setSortDir] = useState<SortDir | "">("desc");
  // ?disposition=<key> (e.g. a Home issue card link) lands the All-issues
  // table pre-filtered to that triage disposition ("none" = unanalyzed).
  const dispParam = sp.get("disposition");
  const [dispFilter, setDispFilter] = useState(
    dispParam !== null && DISP_FILTERS.some((f) => f.key === dispParam) ? dispParam : "");
  const [stateFilter, setStateFilter] = useState("open");
  // Per-column filters (author/pain/repro/subsystem/dups/linked-PRs/labels) —
  // the issue-side analog of PR Explorer's filter spec (#494).
  const [filterSpec, setFilterSpec] = useState<IssueFilterSpec>({});
  const [err, setErr] = useState<string>();
  const [loadingIssues, setLoadingIssues] = useState(true);
  const [loadingDups, setLoadingDups] = useState(dupLinked);
  const [dupsLoaded, setDupsLoaded] = useState(false);
  const [loadingLikely, setLoadingLikely] = useState(dupLinked);
  const [likelyLoaded, setLikelyLoaded] = useState(false);
  // Selection is scoped to the visible page: search/sort/page changes (onQ,
  // onPage, clickSort) clear it so a close can never hit an issue that has
  // scrolled out of view.
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [issueResults, setIssueResults] = useState<Record<number, IssueExecResult>>({});

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    const run = () => {
      api.queryIssues({
        q,
        sort: sortKey || undefined,
        direction: sortDir || undefined,
        disposition: dispFilter || undefined,
        state: stateFilter === "all" ? undefined : stateFilter,
        offset: (page - 1) * PAGE_SIZE,
        limit: PAGE_SIZE,
        ...filterSpec,
      }).then((d) => {
        if (!active) return;
        setRows(d.items);
        setTotal(d.total);
        setLoadingIssues(false);
        // While the backend's PR snapshot is still cold-loading, the rows lack
        // PR states and author stats (and a Linked-PRs sort lacks its
        // merged-fixer ranking) — refetch quietly until they hydrate.
        if (d.pr_states_loading) timer = window.setTimeout(run, 2000);
      }).catch((e) => {
        if (!active) return;
        setErr(String(e));
        setLoadingIssues(false);
      });
    };
    run();
    return () => { active = false; if (timer !== undefined) window.clearTimeout(timer); };
  }, [q, page, sortKey, sortDir, dispFilter, stateFilter, filterSpec]);

  // An in-app navigation can add ?dup= while the page is already mounted
  // (e.g. an issue flyout's cluster chip); a param change lands on the
  // Close-out tab the same way an initial load does.
  const dupParam = sp.get("dup");
  const [prevDup, setPrevDup] = useState(dupParam);
  if (dupParam !== prevDup) {
    setPrevDup(dupParam);
    if (dupParam != null) {
      if (!dupsLoaded) setLoadingDups(true);
      if (!likelyLoaded) setLoadingLikely(true);
      setTab("dups");
    }
  }

  useEffect(() => {
    if (tab !== "dups" || dupsLoaded) return;
    let active = true;
    api.issueDuplicates().then((d) => {
      if (!active) return;
      setGroups(d.groups);
      setDupsLoaded(true);
    }).catch(() => {}).finally(() => {
      if (active) setLoadingDups(false);
    });
    return () => { active = false; };
  }, [tab, dupsLoaded]);

  useEffect(() => {
    if (tab !== "dups" || likelyLoaded) return;
    let active = true;
    api.issuesAlreadyFixed().then((d) => {
      if (!active) return;
      setLikelyItems(d.likely_fixed);
      setLikelyLoaded(true);
    }).catch(() => {}).finally(() => {
      if (active) setLoadingLikely(false);
    });
    return () => { active = false; };
  }, [tab, likelyLoaded]);

  if (err) return <div className="error">Failed to load issues: {err}</div>;
  const dupCount = groups.reduce((n, g) => n + g.dups.length, 0);
  const clickSort = (key: IssueSortKey) => {
    setLoadingIssues(true);
    setPage(1);
    setSelected(new Set());
    setIssueResults({});
    const next = cycleSort({ key: sortKey, dir: sortDir }, key, ISSUE_DESC_FIRST);
    setSortKey(next.key);
    setSortDir(next.dir);
  };
  const changeFilterSpec = (next: IssueFilterSpec) => {
    setLoadingIssues(true);
    setFilterSpec(next);
    setPage(1);
    setSelected(new Set());
    setIssueResults({});
  };

  const toggleOne = (n: number) =>
    setSelected((s) => { const next = new Set(s); if (next.has(n)) next.delete(n); else next.add(n); return next; });
  const toggleAll = (nums: number[], checked: boolean) =>
    setSelected((s) => {
      const next = new Set(s);
      for (const n of nums) { if (checked) next.add(n); else next.delete(n); }
      return next;
    });

  return (
    <div className="issues">
      <div className="detail-head">
        <h1>🐛 Issues</h1>
        <p className="muted">
          GitHub issues from <code>{repoMeta?.repo ?? "the upstream repo"}</code>, clustered by the issue-triage pipeline and
          cross-linked to the PRs that may fix them. Link duplicates to a canonical issue and close them as
          {botLogin} — reversible, gated, and logged like every other write.
        </p>
      </div>

      <SuggestedActions view="issues" />

      <div className="board-controls">
        <div className="segmented">
          <button className={tab === "all" ? "on" : ""} onClick={() => setTab("all")}>
            All issues <span className="count">{total}</span>
          </button>
          <button className={tab === "dups" ? "on" : ""} onClick={() => {
            if (!dupsLoaded) setLoadingDups(true);
            if (!likelyLoaded) setLoadingLikely(true);
            setTab("dups");
          }}>
            Close-out <span className="count">{dupsLoaded ? groups.length : "..."}</span>
          </button>
        </div>
      </div>

      {tab === "dups" ? (
        <>
          <h2 className="closeout-head">Duplicate clusters
            {dupsLoaded && !loadingDups && <span className="muted small"> — {dupCount} duplicate issues across {groups.length} canonical issues</span>}
          </h2>
          {dupsLoaded && dupParam != null && !groups.some((g) => String(g.cluster) === dupParam) && (
            <div className="callout">
              Cluster {dupParam} has no duplicate-triage card — its members aren't curated duplicates
              (a card needs a diagnosed canonical plus confirmed close-as-dup members).
            </div>
          )}
          {loadingDups ? (
            <div className="explorer-loading">
              <span className="spinner explorer-loading-spinner" />
              <span className="explorer-loading-label">Loading duplicate triage…</span>
            </div>
          ) : !dupsLoaded
            ? <div className="callout">Duplicate triage has not loaded yet.</div>
            : groups.length === 0
            ? <div className="callout">No duplicate clusters found. The issue-triage pipeline hasn't surfaced any close-as-dup candidates.</div>
            : <div className="dup-list">{groups.map((g) => <DupGroupCard key={g.canonical} g={g} />)}</div>}

          <h2 className="closeout-head">Likely fixed
            {likelyLoaded && !loadingLikely && <span className="muted small"> — {likelyItems.length} for human review</span>}
          </h2>
          {loadingLikely ? (
            <div className="explorer-loading">
              <span className="spinner explorer-loading-spinner" />
              <span className="explorer-loading-label">Loading the likely-fixed list…</span>
            </div>
          ) : likelyLoaded ? <LikelyFixedSection items={likelyItems} /> : null}
        </>
      ) : (
        <AllIssuesTable
          rows={rows}
          total={total}
          page={page}
          q={q}
          sortKey={sortKey}
          sortDir={sortDir}
          loading={loadingIssues}
          dispFilter={dispFilter}
          stateFilter={stateFilter}
          onQ={(next) => { setLoadingIssues(true); setQ(next); setPage(1); setSelected(new Set()); setIssueResults({}); }}
          onPage={(next) => { setLoadingIssues(true); setPage(next); setSelected(new Set()); setIssueResults({}); }}
          onDispFilter={(d) => { setLoadingIssues(true); setDispFilter(d); setPage(1); setSelected(new Set()); setIssueResults({}); }}
          onStateFilter={(s) => { setLoadingIssues(true); setStateFilter(s); setPage(1); setSelected(new Set()); setIssueResults({}); }}
          onSort={clickSort}
          filterSpec={filterSpec}
          onFilterSpecChange={changeFilterSpec}
          selected={selected}
          results={issueResults}
          onToggle={toggleOne}
          onToggleAll={toggleAll}
        />
      )}
      {tab === "all" && selected.size > 0 && (
        <IssueCloseBar
          selected={[...selected]}
          onResult={(n, res) => setIssueResults((r) => ({ ...r, [n]: res }))}
          onDone={() => setSelected(new Set())}
        />
      )}
    </div>
  );
}
