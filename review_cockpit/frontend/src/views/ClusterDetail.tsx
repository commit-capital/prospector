import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { rehypeLinkifyPrs } from "../rehypeLinkifyPrs";
import { api, type ClusterDetail as CD, type Disposition, type PRRow, type ExecResult, type Suggestion, type AuthorStats, type IssueLink } from "../api";
import { SafetyChip, HumanMergeChip, ConflictChip, DraftChip } from "../components/Chips";
import { ChecksChip } from "../components/ChecksChip";
import { InfoTip } from "../components/InfoTip";
import { clusterStateEntry, dispositionEntry, term } from "../glossary";
import { PRLink } from "../components/PRLink";
import { DiffGrid } from "../components/DiffGrid";
import { AddPrSearch } from "../components/AddPrSearch";
import { AgentSuggestion } from "../components/AgentSuggestion";
import { usePRFlyout } from "../usePRFlyout";
import { makeRowOpen, stopRowOpen } from "../rowOpen";
import { useExec } from "../ExecContext";
import { useRepoMeta } from "../RepoMetaContext";
import { useFreshness } from "../useFreshness";
import { useTableSort } from "../useTableSort";

// A muted "merged / decided" figure beside an author, so reputation is visible
// at the point of action — a 0-of-264 author no longer reads like anyone else.
// Decided = merged + closed-unmerged (open PRs aren't outcomes); full stats on
// hover. Authors with no decided history show an em dash.
function authorRep(a: AuthorStats | null | undefined): ReactNode {
  const merged = a?.merged ?? null;
  const decided = (a?.merged ?? 0) + (a?.closed_unmerged ?? 0);
  if (merged == null || decided === 0) return <span className="muted small"> —</span>;
  const rate = Math.round((100 * merged) / decided);
  const tip = `Decided merge rate ${rate}% (${merged}/${decided} merged)`
    + (a?.open != null ? `\nOpen ${a.open}` : "")
    + (a?.total != null ? `\nTotal PRs ${a.total}` : "")
    + (a?.comments != null ? `\nComments ${a.comments}` : "");
  return <span className="mono small muted" title={tip}> {merged}/{decided}</span>;
}

// "resolved" holds PRs already closed/merged upstream (reconciled, #107) — it
// renders read-only and is excluded from the comment/edit/execute machinery.
const BUCKET_ORDER = ["merge", "request-changes", "close-dup", "close-fixed", "close-stale", "close-oversized", "needs-human", "unanalyzed", "resolved"];
const ACTIVE_BUCKETS = BUCKET_ORDER.filter((b) => b !== "resolved");
const BUCKET_LABEL: Record<string, string> = {
  "merge": "Merge", "request-changes": "Request changes (ask the author)",
  "close-dup": "Close as duplicate", "close-fixed": "Close as already-fixed",
  "close-stale": "Close as stale", "close-oversized": "Close as too big (break up)",
  "needs-human": "Needs human routing",
  "unanalyzed": "Not yet analyzed", "resolved": "Resolved upstream (already closed / merged)",
};
const ACTION_OPTS = ["merge", "request-changes", "close-dup", "close-fixed", "close-stale", "close-oversized", "needs-human", "skip"];
const ACTION_VERB: Record<string, string> = {
  "merge": "Merge", "request-changes": "Request changes on",
  "close-dup": "Close (duplicate)", "close-fixed": "Close (already-fixed)", "close-stale": "Close (stale)",
  "close-oversized": "Close (too big)",
};

// closes + author asks are executable; merge waits for its gate; needs-human is a punt
const defaultApproved = (d: string | null) =>
  ["merge", "request-changes", "close-dup", "close-fixed", "close-stale", "close-oversized"].includes(d ?? "");

// Security severity as a sortable rank — RED (most severe) first on a desc sort.
const SAFETY_RANK: Record<string, number> = { RED: 3, YELLOW: 2, GREEN: 1 };

// rehypeLinkifyPrs tags PR refs as <a class="pr-ref" data-pr="N"> — render those
// through the flyout-aware PRLink; leave any real markdown link as a plain anchor.
const MD_COMPONENTS: Components = {
  a: ({ className, children, href, ...props }) => {
    const dataPr = (props as Record<string, unknown>)["data-pr"];
    if (typeof className === "string" && className.includes("pr-ref") && dataPr != null)
      return <PRLink n={Number(dataPr)} className="pr-ref">{children}</PRLink>;
    return <a className={className} href={href} {...props}>{children}</a>;
  },
};

interface RowState { approved: boolean; override_action: string; override_reason: string; excluded?: boolean }

export default function ClusterDetail() {
  const { id } = useParams();
  const { openPR, addPane } = usePRFlyout();
  const [cd, setCd] = useState<CD>();
  const [state, setState] = useState<Record<number, RowState>>({});
  // suggestion regenerated to match the operator's overridden action (#13)
  const [sugg, setSugg] = useState<Record<number, Suggestion>>({});
  const [execResults, setExecResults] = useState<ExecResult[]>([]);
  // set when a merge failed and the dependent closes were skipped (#188)
  const [aborted, setAborted] = useState<string | null>(null);
  const [executing, setExecuting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [err, setErr] = useState<string>();
  const { botLogin, dryRun, reportResult, pushToast } = useExec();
  const { meta: repoMeta } = useRepoMeta();
  // operator-edited closing/review comments, keyed by PR — lifted out of each
  // AgentSuggestion so the bulk executor posts the edits and "apply to all"
  // can propagate one edit across the duplicate group (#63/#64/#65).
  const [editedComments, setEditedComments] = useState<Record<number, string>>({});
  // A review link can pre-fill these boxes: ?drafts=<base64url of JSON {pr: text}>
  // seeds editedComments once on mount, then the param is stripped so a reload
  // doesn't re-apply a draft the operator has since changed.
  const [searchParams, setSearchParams] = useSearchParams();
  const seededDrafts = useRef(false);
  useEffect(() => {
    if (seededDrafts.current) return;
    const raw = searchParams.get("drafts");
    if (!raw) return;
    seededDrafts.current = true;
    // The blob is gzipped JSON {pr: text}: compression collapses a comment reused
    // across several PRs to a single copy and keeps the link short.
    void (async () => {
      try {
        const bytes = Uint8Array.from(atob(raw.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
        const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
        const map = JSON.parse(await new Response(stream).text()) as Record<string, unknown>;
        const seed: Record<number, string> = {};
        for (const [k, v] of Object.entries(map)) {
          if (typeof v === "string" && Number.isFinite(Number(k))) seed[Number(k)] = v;
        }
        // existing operator edits win over the seed
        setEditedComments((m) => ({ ...seed, ...m }));
      } catch { /* malformed drafts blob — ignore */ }
    })();
    const next = new URLSearchParams(searchParams);
    next.delete("drafts");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);
  // The embedded diff comparison (bottom of page) is driven by the row
  // checkboxes; extraCompare holds any non-member PRs pulled in by number
  // (e.g. a duplicate's canonical target that isn't itself in this cluster).
  const [extraCompare, setExtraCompare] = useState<number[]>([]);
  const [extraRows, setExtraRows] = useState<Record<number, PRRow>>({});

  // ⌘/Ctrl-click a row toggles its checkbox — the selection that drives both the
  // execute plan and "Open in PR Differ". Plain click opens the flyout.
  const toggleApproved = (n: number) =>
    setState((s) => ({ ...s, [n]: { ...s[n], approved: !s[n]?.approved } }));
  // header "select all" — set every row in a bucket at once
  const setBucketApproved = (nums: number[], approved: boolean) =>
    setState((s) => { const next = { ...s }; for (const n of nums) next[n] = { ...next[n], approved }; return next; });
  const rowOpen = makeRowOpen(openPR, addPane, toggleApproved);

  // Reload the cluster and reconcile row state to it. A PR that's now closed/
  // merged upstream drops its `approved` flag: that flag was set when the PR was
  // an active merge/close candidate, and once it slides into the Resolved bucket
  // the checkbox only means "compare this diff" — leaving it checked reads as a
  // stray selection (and leaves the bucket's select-all stuck indeterminate).
  // `announce` toasts any PR that just crossed into Resolved so the page never
  // silently rearranges under the operator; the execute path passes false since
  // it already surfaces its own results.
  const applyCluster = (d: CD, announce = true) => {
    if (announce) {
      const wasOpen = new Set((cd?.prs ?? []).filter((r) => (r.github_state ?? "open") === "open").map((r) => r.number));
      const moved = d.prs.filter((r) => (r.github_state ?? "open") !== "open" && wasOpen.has(r.number));
      if (moved.length)
        pushToast(`${moved.length} PR${moved.length === 1 ? "" : "s"} resolved upstream`, "muted", {
          detail: `${moved.map((r) => `#${r.number}`).join(", ")} ${moved.length === 1 ? "was" : "were"} closed or merged on GitHub — moved to Resolved, no action needed.`,
        });
    }
    setCd(d);
    setState((s) => {
      const next = { ...s };
      for (const r of d.prs)
        if ((r.github_state ?? "open") !== "open" && next[r.number]?.approved)
          next[r.number] = { ...next[r.number], approved: false };
      return next;
    });
  };
  const reloadCd = (announce = true) => {
    if (id) api.cluster(Number(id)).then((d) => applyCluster(d, announce)).catch(() => {});
  };

  // One shared sort across every bucket table (#180) — click any column header
  // to reorder the PRs in all buckets at once. Accessors close over `state` so
  // the Action column sorts by the operator's current per-row choice. No initial
  // column → rows keep their store order until the operator picks one.
  const bucketSort = useTableSort<PRRow>({
    pr: (r) => r.number,
    title: (r) => r.title ?? "",
    author: (r) => r.author ?? "",
    checks: (r) => { const c = r.checks; return c && c.total ? c.passed / c.total : -1; },
    security: (r) => SAFETY_RANK[r.safety ?? ""] ?? 0,
    action: (r) => state[r.number]?.override_action ?? "",
    state: (r) => r.github_state ?? "open",
    pain: (r) => r.pain_score ?? 0,
  }, { descFirst: ["checks", "security", "pain"], tiebreak: (a, b) => a.number - b.number });

  const [rerunLog, setRerunLog] = useState<string[]>([]);
  const [rerunning, setRerunning] = useState(false);
  const rerunEs = useRef<EventSource | null>(null);

  // Per-PR security re-run (#56): one at a time, streamed like the cluster re-run.
  const [secRun, setSecRun] = useState<{ pr: number; log: string[]; running: boolean } | null>(null);
  const secEs = useRef<EventSource | null>(null);
  useEffect(() => () => { rerunEs.current?.close(); secEs.current?.close(); }, []);

  // Attaches (start or reattach, #683) an SSE stream for the per-PR
  // security-review job to `secRun`. The job runs to completion server-side
  // regardless of this page's lifetime — closing this connection only drops
  // this view of it.
  const attachSecurityJob = (url: string, pr: number, initialLine: string) => {
    secEs.current?.close();
    setSecRun({ pr, log: [initialLine], running: true });
    const es = new EventSource(url);
    secEs.current = es;
    es.addEventListener("log", (e: MessageEvent) =>
      setSecRun((s) => (s ? { ...s, log: [...s.log, e.data] } : s)));
    es.addEventListener("done", (e: MessageEvent) => {
      const { status } = JSON.parse(e.data);
      setSecRun((s) => (s ? { ...s, log: [...s.log, `■ ${status}`], running: false } : s));
      es.close();
      pushToast(`#${pr} · Security review ${status}`, status === "done" ? "green" : "red",
        { detail: status === "done" ? "verdict reloaded below" : "the phase exited non-zero — see the log" });
      reloadCd();  // reload with fresh verdict
    });
    es.onerror = () => {
      es.close();
      setSecRun((s) => (s ? { ...s, running: false } : s));
      pushToast(`#${pr} · Security review — connection lost`, "red");
    };
  };

  const runSecurity = (pr: number) => {
    if (secRun?.running) return;
    pushToast(`▶ #${pr} · Security review — running…`, "muted");
    attachSecurityJob(`/api/jobs/run/security-review?pr=${pr}`, pr, `▶ security review of #${pr}…`);
  };

  // Same start-or-reattach pattern for the cluster-scoped re-run.
  const attachRerun = (url: string, initialLine: string) => {
    rerunEs.current?.close();
    setRerunLog([initialLine]);
    setRerunning(true);
    const es = new EventSource(url);
    rerunEs.current = es;
    es.addEventListener("log", (e: MessageEvent) => setRerunLog((l) => [...l, e.data]));
    es.addEventListener("done", (e: MessageEvent) => {
      const { status } = JSON.parse(e.data);
      setRerunLog((l) => [...l, `■ ${status}`]);
      es.close();
      setRerunning(false);
      pushToast(`cluster ${id} · re-run ${status}`, status === "done" ? "green" : "red",
        { detail: status === "done" ? "analysis reloaded below" : "the phase exited non-zero — see the log" });
      reloadCd();  // reload with fresh analysis
    });
    es.onerror = () => {
      es.close();
      setRerunning(false);
      pushToast(`cluster ${id} · re-run — connection lost`, "red");
    };
  };

  const rerun = () => {
    if (!id || rerunning) return;
    pushToast(`▶ cluster ${id} · re-running…`, "muted");
    attachRerun(`/api/jobs/run/triage-cluster?cluster=${encodeURIComponent(id)}`, `▶ re-running cluster ${id}…`);
  };

  useEffect(() => {
    if (!id) return;
    api.cluster(Number(id)).then((d) => {
      setCd(d);
      const init: Record<number, RowState> = {};
      for (const r of d.prs) {
        const act = r.disposition ?? "needs-human";
        // don't pre-approve a PR that's already closed/merged upstream (#107)
        const open = (r.github_state ?? "open") === "open";
        init[r.number] = { approved: open && defaultApproved(r.disposition), override_action: act, override_reason: "" };
      }
      setState(init);

      // #683: a re-run or security-review job keeps going server-side for as
      // long as it takes, independent of this page — reattach to one already
      // in flight for this cluster (or one of its member PRs) instead of
      // showing nothing just because a fresh load lost the original stream.
      const memberPrs = new Set(d.prs.map((r) => r.number));
      api.jobsList().then((jd) => {
        const running = jd.jobs.filter((j) => j.status === "running");
        const clusterJob = running.filter((j) => j.kind === "triage-cluster" && j.cluster === Number(id))
          .sort((a, b) => b.id - a.id)[0];
        if (clusterJob) {
          attachRerun(`/api/jobs/${clusterJob.id}/stream`,
            `↻ reattached to job #${clusterJob.id} — replaying its output so far…`);
        }
        const secJob = running.filter((j) => j.kind === "security-review" && j.pr != null && memberPrs.has(j.pr))
          .sort((a, b) => b.id - a.id)[0];
        if (secJob?.pr != null) {
          attachSecurityJob(`/api/jobs/${secJob.id}/stream`, secJob.pr,
            `↻ reattached to job #${secJob.id} — replaying its output so far…`);
        }
      }).catch(() => {});
    }).catch((e) => setErr(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- attachRerun/attachSecurityJob are stable per render and re-running this on their identity would refetch the cluster needlessly
  }, [id]);

  // Background freshness check (#25) doubles as reconciliation (#107): when a PR
  // we're showing as active turns out closed/merged upstream, the server
  // persists that, so reload the cluster to move it into the resolved bucket.
  const prNums = useMemo(() => (cd?.prs ?? []).map((r) => r.number), [cd]);
  // member PR numbers — lets the rationale linkify bare "1234" refs, not just "#1234"
  const knownPrs = useMemo(() => new Set(prNums), [prNums]);
  const fresh = useFreshness(prNums);
  useEffect(() => {
    if (!id || !cd) return;
    const movedToResolved = (cd.prs || []).some((r) =>
      (r.github_state ?? "open") === "open" &&
      (fresh.byPr[r.number] || []).some((d) =>
        d.kind === "merged" || (d.kind === "state" && (d.now === "closed" || d.now === "merged"))));
    if (movedToResolved) reloadCd();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fresh]);

  // header meta (title/author/url) for compare columns that aren't cluster members
  useEffect(() => {
    const need = extraCompare.filter((n) => !extraRows[n]);
    if (!need.length) return;
    api.queryPrs({ numbers: need }, { limit: need.length }).then((r) =>
      setExtraRows((m) => ({ ...m, ...Object.fromEntries(r.items.map((it) => [it.number, it])) }))).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [extraCompare.join(",")]);

  // The comparison can fetch several diffs, so only mount it once it scrolls
  // into view — the cluster page stays light on load even when PRs are
  // pre-approved (and thus pre-selected for comparison).
  const diffSecRef = useRef<HTMLElement>(null);
  const [diffSeen, setDiffSeen] = useState(false);
  useEffect(() => {
    const el = diffSecRef.current;
    if (diffSeen || !el) return;
    const ob = new IntersectionObserver(
      (es) => { if (es.some((e) => e.isIntersecting)) { setDiffSeen(true); ob.disconnect(); } },
      { rootMargin: "300px" });
    ob.observe(el);
    return () => ob.disconnect();
  }, [diffSeen, cd]);

  const update = (pr: number, patch: Partial<RowState>) =>
    setState((s) => ({ ...s, [pr]: { ...s[pr], ...patch } }));

  // Exclude a PR from the cluster (#181): drop it from the actionable set before
  // running bulk actions. Excluding also un-approves it so it leaves the execute
  // plan and diff comparison; re-include puts it back in its bucket.
  const toggleExcluded = (n: number) =>
    setState((s) => {
      const excluded = !s[n]?.excluded;
      return { ...s, [n]: { ...s[n], excluded, approved: excluded ? false : s[n]?.approved } };
    });

  // Reload the cluster from the store. After a single PR is closed/reopened from
  // its row, the live-state overlay has the new state, so this moves a freshly
  // closed PR into the "resolved" (Already Closed) bucket and out of the
  // actionable set — instead of it lingering in bulk-close (#187).
  const reloadCluster = reloadCd;

  // Add/remove a PR from the embedded comparison. A cluster member toggles its
  // row checkbox; a non-member rides in extraCompare.
  const addCompare = (n: number) => {
    if (!n) return;
    if ((cd?.prs ?? []).some((r) => r.number === n)) update(n, { approved: true });
    else setExtraCompare((xs) => (xs.includes(n) ? xs : [...xs, n]));
  };
  const removeCompare = (n: number) => {
    if ((cd?.prs ?? []).some((r) => r.number === n)) update(n, { approved: false });
    else setExtraCompare((xs) => xs.filter((x) => x !== n));
  };

  // Merge rows whose gate is blocked solely on a current YELLOW verdict: the
  // typed reason is sent with the merge and logged to the store as the verdict's
  // override (the backend refuses the merge without it).
  const needsReason = (r: PRRow) => {
    const st = state[r.number];
    return st?.approved && st.override_action === "merge" && !!r.merge_gate?.overridable;
  };

  // A PR that no longer merges cleanly — from the stored/overlaid mergeable
  // signal or a live "now has conflicts" freshness divergence (#189). A merge
  // can't succeed on it, so it's surfaced and blocked from the merge action.
  const hasConflict = (r: PRRow) =>
    !!r.signals?.conflicts || (fresh.byPr[r.number] || []).some((d) => d.kind === "conflicts");
  const liveConflict = (r: PRRow) => (fresh.byPr[r.number] || []).some((d) => d.kind === "conflicts");

  const EXECUTABLE = ["merge", "request-changes", "close-dup", "close-fixed", "close-stale", "close-oversized"];
  const executable = () => (cd?.prs ?? []).filter((r) => {
    const st = state[r.number];
    // already-closed/merged PRs may be checked for diff comparison, but are
    // never re-executed — only open PRs are actionable. Excluded PRs (#181) are
    // dropped from the set entirely. A conflicted PR can't be merged (#189).
    if (st?.override_action === "merge" && hasConflict(r)) return false;
    return (r.github_state ?? "open") === "open" && st?.approved && !st?.excluded
      && EXECUTABLE.includes(st.override_action);
  });

  // the comment a PR would actually post: the operator's edit, else the
  // regenerated (overridden-action) suggestion, else the stored one. Grouping
  // keys off this so seeded/edited duplicates collapse, not just canned ones.
  const effectiveComment = (r: PRRow): string | undefined =>
    editedComments[r.number] ?? (sugg[r.number] ?? r.suggestion)?.bot_comment ?? undefined;

  // identical comments collapse to "same as #X" — first occurrence shows full
  const sameAs = useMemo(() => {
    const seen = new Map<string, number>();
    const out: Record<number, number> = {};
    for (const b of ACTIVE_BUCKETS) for (const r of cd?.buckets[b] ?? []) {
      const c = effectiveComment(r);
      if (!c) continue;
      if (seen.has(c)) out[r.number] = seen.get(c)!; else seen.set(c, r.number);
    }
    return out;
  }, [cd, editedComments, sugg]); // eslint-disable-line react-hooks/exhaustive-deps -- effectiveComment reads these

  // PRs grouped by the exact comment they'd post — powers "apply to all
  // duplicates" so one edit covers the whole group (#64).
  const commentGroups = useMemo(() => {
    const byComment = new Map<string, number[]>();
    for (const b of ACTIVE_BUCKETS) for (const r of cd?.buckets[b] ?? []) {
      const c = effectiveComment(r);
      if (!c) continue;
      if (!byComment.has(c)) byComment.set(c, []);
      byComment.get(c)!.push(r.number);
    }
    const out: Record<number, number[]> = {};
    for (const prs of byComment.values()) for (const p of prs) out[p] = prs;
    return out;
  }, [cd, editedComments, sugg]); // eslint-disable-line react-hooks/exhaustive-deps -- effectiveComment reads these

  // header meta for every compare column: cluster members from cd.prs, plus any
  // non-members fetched into extraRows.
  const compareRows = useMemo(() => {
    const m: Record<number, PRRow> = { ...extraRows };
    for (const r of cd?.prs ?? []) m[r.number] = r;
    return m;
  }, [cd, extraRows]);

  // pre-flight summary of exactly what will fire
  const plan = () => {
    const todo = executable();
    const byAction: Record<string, PRRow[]> = {};
    const flagged: PRRow[] = [];
    for (const r of todo) {
      const act = state[r.number].override_action;
      (byAction[act] ??= []).push(r);
      if (r.suggestion?.needs_verify || (act === "merge" && r.safety !== "GREEN")) flagged.push(r);
    }
    const merges = byAction["merge"]?.length ?? 0;
    return { todo, byAction, flagged, merges };
  };

  const execute = async () => {
    if (!cd) return;
    const todo = executable();
    if (todo.length === 0) return;
    setConfirming(false);
    setExecuting(true);
    setExecResults([]);
    setAborted(null);
    // One batched request for the whole group → one activity-log commit (not one
    // per PR). Build each PR's payload from exactly what the operator sees/edited:
    // their edit if any, else the suggestion's verbatim bot_comment (the
    // regenerated one when the action was overridden) — NOT the stale
    // r.suggestion.comment, which is unset for closes and let the edit get
    // silently dropped (#65). The raw disposition is normalized server-side, which
    // also runs merges first and skips the dependent closes if one fails (#188).
    const items = todo.map((r) => {
      const act = state[r.number].override_action as Disposition;
      const sg = sugg[r.number] ?? r.suggestion;
      const postText = editedComments[r.number] ?? sg?.bot_comment ?? undefined;
      return {
        pr: r.number,
        action: act,
        comment: postText,
        reason: state[r.number].override_reason || undefined,
        canonical: r.proposed_action?.canonical ?? undefined,
        upstream_pr: r.proposed_action?.upstream_pr ?? undefined,
        upstream_commit: r.proposed_action?.upstream_commit ?? undefined,
        upstream_date: r.proposed_action?.upstream_date ?? undefined,
        method: act === "merge" ? "squash" : undefined,
        tags: act === "request-changes" ? ["needs-revision", "agent-suggested"] : undefined,
      };
    });
    try {
      await api.executeCluster(items, dryRun,
        (res) => { setExecResults((prev) => [...prev, res]); reportResult(res); },
        (_summary, aborted) => setAborted(aborted ?? null));  // #188 skip message, when any
    } finally {
      // a network/stream failure aborts the whole batch; clear the busy state so
      // the UI never locks, then refresh so dispositions/run-state reflect what
      // landed (#69)
      setExecuting(false);
      reloadCd(false);
    }
  };

  // flipping dry-run clears stale preview/exec rows so LIVE never shows a
  // leftover dry-run result set (#67)
  useEffect(() => { setExecResults([]); setConfirming(false); setAborted(null); }, [dryRun]); // eslint-disable-line react-hooks/set-state-in-effect -- clear stale preview/exec rows on dry-run flip

  if (err) return <div className="error">Failed to load: {err}</div>;
  if (!cd) return <div className="muted pad">Loading…</div>;

  const approvedCount = (cd.prs ?? []).filter((r) =>
    (r.github_state ?? "open") === "open" && state[r.number]?.approved
    && !state[r.number]?.excluded && state[r.number]?.override_action !== "skip").length;
  // every checked row — open or closed — stacks into the embedded comparison
  // below, plus any non-member PRs added by number (capped at CAP columns).
  const checkedPrs = (cd.prs ?? []).filter((r) => state[r.number]?.approved && !state[r.number]?.excluded).map((r) => r.number);
  const CAP = 6;
  const comparePrs = [...new Set([...checkedPrs, ...extraCompare])];
  const shownPrs = comparePrs.slice(0, CAP);
  const overflow = comparePrs.length - shownPrs.length;

  return (
    <div className="detail">
      <div className="detail-head">
        <Link to="/" className="back">← Clusters</Link>
        <h1>Cluster {cd.cluster_id}: {cd.root_problem}</h1>
        <p className="muted">
          <InfoTip entry={clusterStateEntry(cd.state)} cue={false} focusable={false}>
            <span className="chip chip-muted">{cd.state}</span>
          </InfoTip>
          {cd.outcome && <> · outcome: <InfoTip entry={term(`outcome.${cd.outcome}`)}><b>{cd.outcome}</b></InfoTip></>}
          {cd.analyzed_at && <> · analyzed {cd.analyzed_at.slice(0, 10)}</>}
        </p>
        {cd.rationale_summary && (
          <p className="rationale-tldr"><span className="tldr-label">TL;DR</span> {cd.rationale_summary}</p>
        )}
        {cd.rationale && (
          <div className="rationale markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeLinkifyPrs(knownPrs)]} components={MD_COMPONENTS}>
              {cd.rationale}
            </ReactMarkdown>
          </div>
        )}
        {cd.notes && <p className="muted small">📝 {cd.notes}</p>}
        <button className="btn-secondary sm" disabled={rerunning} onClick={rerun}>
          {rerunning ? "Re-running…" : "↻ Re-run analysis"}
        </button>
        {rerunLog.length > 0 && (
          <div className="joblog" style={{ marginTop: 8 }}>
            {rerunLog.map((l, i) => <div key={i} className="logline">{l}</div>)}
          </div>
        )}
      </div>

      {BUCKET_ORDER.filter((b) => cd.buckets[b]?.some((r) => !state[r.number]?.excluded)).map((b) => {
        // excluded PRs (#181) drop out of their bucket and into the Excluded
        // section below; an emptied bucket disappears.
        const visible = cd.buckets[b].filter((r) => !state[r.number]?.excluded);
        const bucketNums = visible.map((r) => r.number);
        const selAll = <SelectAllTh nums={bucketNums} approved={(n) => !!state[n]?.approved} onToggle={(t) => setBucketApproved(bucketNums, t)} />;
        return b === "resolved" ? (
        <section className="bucket" key={b}>
          <h3><InfoTip entry={dispositionEntry(b as Disposition)} cue={false} focusable={false}>{BUCKET_LABEL[b]}</InfoTip> <span className="count">{visible.length}</span></h3>
          <p className="muted small">Closed or merged upstream — reconciled into our records, so they've left the active queue. No action needed.</p>
          <table className="grid compact sortable">
            <thead><tr>{selAll}
              <th {...bucketSort.thProps("pr")}>PR{bucketSort.indicator("pr")}</th>
              <th {...bucketSort.thProps("title")}>Title{bucketSort.indicator("title")}</th>
              <th {...bucketSort.thProps("author")}>Author{bucketSort.indicator("author")}</th>
              <th {...bucketSort.thProps("state")}>State{bucketSort.indicator("state")}</th>
            </tr></thead>
            <tbody>
              {bucketSort.sorted(visible).map((r) => {
                const st = state[r.number];
                const ro = rowOpen(r.number);
                return (
                  <tr key={r.number} {...ro} className={`${ro.className} ${st?.approved ? "" : "row-skipped"}`}>
                    <td onClick={stopRowOpen}><input type="checkbox" title="Compare this closed PR's diff below" checked={!!st?.approved} onChange={(e) => update(r.number, { approved: e.target.checked })} /></td>
                    <td className="mono"><PRLink n={r.number} /></td>
                    <td>{r.title || <span className="muted">(not in store)</span>}</td>
                    <td className="muted small">{r.author}</td>
                    <td><span className={`chip ${r.github_state === "merged" ? "chip-blue" : "chip-muted"}`}>{r.github_state === "merged" ? "🔀 merged" : "✓ closed"}</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
        ) : (
        <section className="bucket" key={b}>
          <h3><InfoTip entry={dispositionEntry(b as Disposition)} cue={false} focusable={false}>{BUCKET_LABEL[b]}</InfoTip> <span className="count">{visible.length}</span></h3>
          <table className="grid compact sortable">
            <thead><tr>{selAll}
              <th {...bucketSort.thProps("pr")}>PR{bucketSort.indicator("pr")}</th>
              <th {...bucketSort.thProps("title")}>Title / rationale{bucketSort.indicator("title")}</th>
              <th {...bucketSort.thProps("author")}>Author{bucketSort.indicator("author")}</th>
              <th {...bucketSort.thProps("pain")} title="Community Pain Score — linked-issue pain + PR comments + reactions. Click to sort: most painful first.">Pain{bucketSort.indicator("pain")}</th>
              <th {...bucketSort.thProps("checks")} title="How thoroughly vetted — hover a cell for the per-check breakdown. Click to sort.">Checks{bucketSort.indicator("checks")}</th>
              <th {...bucketSort.thProps("security")} title="Deep security review verdict (runs on clean merge candidates). Click to sort: most severe first.">Security{bucketSort.indicator("security")}</th>
              <th {...bucketSort.thProps("action")}>Action{bucketSort.indicator("action")}</th>
            </tr></thead>
            <tbody>
              {bucketSort.sorted(visible).map((r) => {
                const st = state[r.number];
                const stop = stopRowOpen;
                const ro = rowOpen(r.number);
                return (
                  <tr key={r.number} {...ro} className={`${ro.className} ${st?.approved ? "" : "row-skipped"}`}>
                    <td onClick={stop}><input type="checkbox" checked={!!st?.approved} onChange={(e) => update(r.number, { approved: e.target.checked })} /></td>
                    <td className="mono"><PRLink n={r.number} /></td>
                    <td>
                      {r.title || <span className="muted">(not in store)</span>}
                      {r.draft && <> <DraftChip draft={r.draft} /></>}
                      {r.human_merge?.required && <> <HumanMergeChip hm={r.human_merge} /></>}
                      {hasConflict(r) && <> <ConflictChip live={liveConflict(r)} /></>}
                      {r.proposed_action?.rationale && <div className="muted small">↳ {r.proposed_action.rationale}</div>}
                      {!!r.proposed_action?.asks?.length && (
                        <div className="muted small">asks: {r.proposed_action.asks.join(" · ")}</div>
                      )}
                      {!!r.issues?.length && (() => {
                        const fmt = (i: IssueLink): string => `#${i.issue}${i.pain != null ? ` (pain ${i.pain.toFixed(2)})` : ""}`;
                        const fixes = r.issues.filter((i) => i.how === "explicit");
                        const related = r.issues.filter((i) => i.how !== "explicit");
                        return (
                          <>
                            {!!fixes.length && (
                              <div className="muted small" title="Issues this PR explicitly declares it fixes (Fixes/Closes #N)">
                                🔗 Fixes {fixes.map(fmt).join(", ")}
                              </div>
                            )}
                            {!!related.length && (
                              <details className="muted small" onClick={stop}>
                                <summary title="PRs touching the same subsystem as these issues — a discovery hint, not a declared fix. Excluded from the pain score.">
                                  {related.length} related issue{related.length === 1 ? "" : "s"} (same subsystem)
                                </summary>
                                {related.map(fmt).join(", ")}
                              </details>
                            )}
                          </>
                        );
                      })()}
                      {needsReason(r) && (
                        <input className="reason" onClick={stop} placeholder="⚠ security YELLOW — override reason required to merge (logged, never posted)…"
                          value={st?.override_reason ?? ""} onChange={(e) => update(r.number, { override_reason: e.target.value })} />
                      )}
                      <span onClick={stop}><AgentSuggestion pr={r.number} suggestion={sugg[r.number] ?? r.suggestion} compact
                        actionChanged={!!sugg[r.number]}
                        commentSameAs={sugg[r.number] ? undefined : sameAs[r.number]} humanMerge={r.human_merge}
                        editedComment={editedComments[r.number]}
                        onActed={reloadCluster}
                        onEdit={(t) => setEditedComments((m) => { const c = { ...m }; if (t == null) delete c[r.number]; else c[r.number] = t; return c; })}
                        onApplyToAll={(text) => setEditedComments((m) => { const c = { ...m }; for (const n of (commentGroups[r.number] ?? [r.number])) c[n] = text; return c; })}
                        applyToAllCount={(commentGroups[r.number]?.length ?? 1) - 1} /></span>
                    </td>
                    <td className="muted small">{r.author}{r.trusted_author && <InfoTip entry={term("trusted")} cue={false} focusable={false}><span className="chip chip-gold sm">trusted</span></InfoTip>}{authorRep(r.author_stats)}</td>
                    <td className="mono small" title={r.pain_breakdown ? `Issue pain: ${r.pain_breakdown.issue_pain.toFixed(2)} (${r.pain_breakdown.linked_issues} issue${r.pain_breakdown.linked_issues === 1 ? "" : "s"})\nPR comments: ${r.pain_breakdown.pr_comments}\nPR reactions: ${r.pain_breakdown.pr_reactions}` : "Community Pain Score: no data"}>
                      {r.pain_score != null && r.pain_score > 0 ? r.pain_score.toFixed(2) : "—"}
                    </td>
                    <td><ChecksChip c={r.checks} /></td>
                    <td onClick={stop}>
                      <SafetyChip v={r.safety} findings={r.safety_titles} />
                      {r.safety && r.safety_fresh === false && <InfoTip entry={term("freshness.stale")} cue={false} focusable={false}><span className="chip chip-muted sm">stale</span></InfoTip>}
                      {/* Run the deep review on demand for any PR lacking a current verdict (#56).
                          The engine is gate-free — it reviews whatever it's handed, not just merge candidates. */}
                      {(!r.safety || r.safety_fresh === false) && (
                        <button className="btn-secondary sm" style={{ marginLeft: 6 }}
                          disabled={secRun?.running}
                          title="Run the 3-lens adversarial security review on this PR now"
                          onClick={() => runSecurity(r.number)}>
                          {secRun?.pr === r.number && secRun.running ? "Running…" : "↻ Run"}
                        </button>
                      )}
                      {secRun?.pr === r.number && secRun.log.length > 1 && (
                        <div className="joblog" style={{ marginTop: 6 }}>
                          {secRun.log.map((l, i) => <div key={i} className="logline">{l}</div>)}
                        </div>
                      )}
                    </td>
                    <td onClick={stop} className="action-cell">
                      <select className="action-select" value={st?.override_action} onChange={(e) => {
                        const act = e.target.value;
                        update(r.number, { override_action: act });
                        // the edit was for the old action's wording — drop it so we
                        // don't post a close comment on a request-changes, etc. (#65)
                        setEditedComments((m) => { const c = { ...m }; delete c[r.number]; return c; });
                        // regenerate the suggested comment to match the new action (#13)
                        if (act === "skip") { setSugg((m) => { const c = { ...m }; delete c[r.number]; return c; }); }
                        else api.suggestForAction(r.number, act).then((s) => setSugg((m) => ({ ...m, [r.number]: s }))).catch(() => {});
                      }}>
                        {ACTION_OPTS.map((o) => <option key={o} value={o}
                          disabled={o === "merge" && hasConflict(r)}
                          title={o === "skip" ? "Leave this PR untouched — no action, stays open."
                            : o === "merge" && hasConflict(r) ? "Can't merge — this PR now has conflicts; the author must rebase."
                            : dispositionEntry(o as Disposition)?.meaning}>{o}</option>)}
                      </select>
                      {hasConflict(r) && state[r.number]?.override_action === "merge" && (
                        <div className="muted small" style={{ color: "var(--red)" }}>⚠ can't merge — has conflicts</div>
                      )}
                      {st?.override_action === "close-dup" && r.proposed_action?.canonical &&
                        <span className="muted small" style={{ whiteSpace: "nowrap" }}>dup #{r.proposed_action.canonical}</span>}
                      <button className="link-btn exclude-btn" title="Exclude this PR from the cluster — drops it from bulk actions. Re-include it from the Excluded list below."
                        onClick={() => toggleExcluded(r.number)}>✕ exclude</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
        );
      })}

      {(() => {
        // PRs the operator excluded from the cluster (#181) — dropped from bulk
        // actions, listed here so they're visible and one click puts them back.
        const excluded = (cd.prs ?? []).filter((r) => state[r.number]?.excluded);
        if (!excluded.length) return null;
        return (
          <section className="bucket excluded-bucket">
            <h3>Excluded from this cluster <span className="count">{excluded.length}</span></h3>
            <p className="muted small">Dropped from the actionable set — bulk actions skip these. Re-include to put one back in its bucket.</p>
            <table className="grid compact">
              <thead><tr><th>PR</th><th>Title</th><th>Author</th><th></th></tr></thead>
              <tbody>
                {excluded.map((r) => (
                  <tr key={r.number} className="row-skipped">
                    <td className="mono"><PRLink n={r.number} /></td>
                    <td>{r.title || <span className="muted">(not in store)</span>}</td>
                    <td className="muted small">{r.author}</td>
                    <td><button className="btn-secondary sm" title="Put this PR back into its bucket" onClick={() => toggleExcluded(r.number)}>↩ re-include</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        );
      })()}

      <section className="emit-form">
        <h3>Execute the plan</h3>
        {!confirming ? (
          <>
            <div className="emit-bar">
              <button className={`btn-primary ${!dryRun ? "btn-live" : ""}`}
                onClick={() => setConfirming(true)} disabled={executing || executable().length === 0}>
                {executing ? "Working…" : `Review & ${dryRun ? "dry-run" : "run"} ${executable().length} action${executable().length === 1 ? "" : "s"} →`}
              </button>
              <span className="muted small">{approvedCount} approved · all actions run as {botLogin} · merges gated (clean + GREEN)</span>
            </div>
            <div className="muted small">
              “needs-human” / “skip” are punts — left open, no action. Nothing fires until you confirm on the next screen.
            </div>
          </>
        ) : (() => {
          const { byAction, flagged, merges } = plan();
          const n = executable().length;
          return (
            <div className={`preflight ${!dryRun ? "preflight-live" : ""}`}>
              <div className="preflight-head">
                {dryRun
                  ? <><b>Dry run</b> — previews {n} action{n === 1 ? "" : "s"} without touching GitHub.</>
                  : <><b className="live-word">● LIVE</b> — about to run {n} action{n === 1 ? "" : "s"} on <b>{repoMeta?.repo ?? "the upstream repo"}</b>.</>}
              </div>
              <ul className="preflight-list">
                {Object.entries(byAction).map(([act, rows]) => (
                  <li key={act}>
                    <b>{ACTION_VERB[act] ?? act} {rows.length}</b>
                    {act === "merge"
                      ? <span className="rev-badge rev-perm"> ⚠ PERMANENT — cannot be undone</span>
                      : <span className="rev-badge rev-ok"> reversible</span>}
                    <span className="muted small"> — {rows.map((r) => `#${r.number}`).join(", ")}</span>
                  </li>
                ))}
              </ul>
              {flagged.length > 0 && (
                <div className="preflight-warn">
                  ⚠ {flagged.length} of these {flagged.length === 1 ? "was" : "were"} flagged <b>needs your check</b> — verify before firing:{" "}
                  {flagged.map((r, i) => (
                    <span key={r.number}>{i > 0 && ", "}<a onClick={(e) => { e.stopPropagation(); openPR(r.number); }}>#{r.number}</a></span>
                  ))}
                </div>
              )}
              {merges > 0 && Object.keys(byAction).some((a) => a !== "merge") && (
                <div className="muted small" style={{ marginTop: 6 }}>
                  ↪ Merges run <b>first</b>. If any merge fails, the closes/asks are skipped — so nothing is closed as a duplicate of a PR that didn't merge.
                </div>
              )}
              <div className="preflight-actions">
                <button className="btn-secondary" onClick={() => setConfirming(false)} disabled={executing}>← Back to review</button>
                <button className={`btn-primary ${!dryRun ? "btn-live" : ""}`} onClick={execute} disabled={executing}>
                  {executing ? "Working…" : dryRun
                    ? `Run dry-run preview (${n})`
                    : `● Confirm & run ${n} live action${n === 1 ? "" : "s"}${merges ? ` · ${merges} permanent merge${merges === 1 ? "" : "s"}` : ""}`}
                </button>
              </div>
            </div>
          );
        })()}
        {(execResults.length > 0 || aborted) && (
          <div className="exec-results">
            <h4>{dryRun ? "Dry-run preview" : "Execution results"}</h4>
            {aborted && <div className="preflight-warn">⛔ {aborted}</div>}
            {execResults.map((r, i) => (
              <div key={i} className={`exec-row exec-${r.status}`}>
                <span className="mono">#{r.pr}</span>
                <span className={`chip chip-${r.status === "executed" || r.status === "merged" ? "green" : r.status === "error" || r.status === "blocked" ? "red" : "muted"}`}>{r.status}</span>
                <span className="muted small">{r.detail}</span>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="cluster-diff" ref={diffSecRef}>
        <div className="cluster-diff-head">
          <h3>Compare diffs</h3>
          <span className="muted small">{comparePrs.length || "no"} selected{overflow > 0 ? ` · showing first ${CAP}` : ""}</span>
          <div className="cluster-diff-tools">
            <AddPrSearch onAdd={addCompare} exclude={comparePrs} placeholder="add a PR by number (e.g. a canonical)…" />
            {comparePrs.length > 0 && (
              <Link className="link-btn" to={`/differ?prs=${comparePrs.join(",")}`} title="Open this comparison full-screen in the PR Differ">pop out ↗</Link>
            )}
          </div>
        </div>
        {comparePrs.length === 0 ? (
          <div className="muted cluster-diff-empty">
            Tick the checkbox on any PR above — open <b>or closed</b> — to stack its diff here, or add one by number.
            The same file lines up across every column, blank where a PR doesn't touch it.
          </div>
        ) : diffSeen ? (
          <DiffGrid prs={shownPrs} rows={compareRows} onRemove={removeCompare} />
        ) : (
          <div className="muted cluster-diff-empty">{comparePrs.length} PR{comparePrs.length === 1 ? "" : "s"} selected — scroll down to load the comparison…</div>
        )}
      </section>
    </div>
  );
}

/** Header checkbox that checks/unchecks every row in its bucket, showing the
 *  usual indeterminate dash when only some rows are checked. */
function SelectAllTh({ nums, approved, onToggle }: {
  nums: number[];
  approved: (n: number) => boolean;
  onToggle: (target: boolean) => void;
}) {
  const all = nums.length > 0 && nums.every(approved);
  const some = nums.some(approved);
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { if (ref.current) ref.current.indeterminate = some && !all; }, [some, all]);
  return (
    <th className="selall">
      <input ref={ref} type="checkbox" checked={all} disabled={!nums.length}
        onChange={() => onToggle(!all)}
        title={all ? "Uncheck all in this group" : "Check all in this group"} />
    </th>
  );
}
