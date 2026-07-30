"""GitHub Issues, folded into the cockpit (#192).

A read-only projection over the issue store (issue_triage/store/): the migrated
issues, their dedup clusters, pain, repro grades, and the issue<->PR candidate
links. Assembles the rows + the curated close-as-dup worklist the Issues view
shows, and cross-links each issue to the PRs that may address it.

Writes (close-as-dup) go through the cockpit executor as the configured bot, gated by
issue_gates.close_dup_eligibility (via close_dup_gate) and logged like every other
upstream write (executor.close_issue).
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import profile
from pipeline import storekit
from pipeline.settings import REPO
from app.backend import issue_data
from app.backend.filters import num_cmp
from app.backend.safety_guard import run

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue, IssueCluster

# The issue store root. None resolves the shared store (TRIAGE_STORE_URL), the
# same source the rest of the cockpit reads; tests override it with a seeded tmp
# path, which an explicit root maps to a local SQLite file.
STORE_ROOT: Path | None = None
_synced_store_root: Path | None = None


def _store():
    _sync_store_root()
    return issue_data.store()


def cached_issues() -> dict[int, Issue]:
    """Every issue in the store from the cockpit's cached light snapshot
    (candidate PR links omitted) — for whole-store scans that need no links."""
    _sync_store_root()
    return issue_data.issues()


def cached_runs() -> list[storekit.RunRecord]:
    """The issue pipeline's runs ledger, oldest first, from the cockpit's
    cached snapshot."""
    _sync_store_root()
    return issue_data.runs()


def _sync_store_root() -> None:
    global _synced_store_root, _dup_groups_cache
    normalized = Path(STORE_ROOT) if STORE_ROOT is not None else None
    if normalized != _synced_store_root:
        issue_data.set_store_root(normalized)
        _dup_groups_cache = None
        _synced_store_root = normalized


def reflect_issue_state(n: int, state: str, state_reason: str | None = None) -> None:
    """Record our own issue state change and its reason into the shared issue store
    and refresh this cockpit's snapshot, so the Issues view and close-as-dup
    worklist reflect it instantly. The issue-side analog of the PR executor's
    _reflect_state. A no-op when the issue has no store row."""
    st = _store()
    if st.load_issue(int(n)) is not None:
        st.edit_issue(int(n)).record_live_state(state, state_reason)
    issue_data.refresh()


def issue_state(n: int) -> str | None:
    """The issue's last-known state in our store (``open``/``closed``), or None
    when we have no row for it."""
    iss = _store().load_issue(int(n))
    return iss.state if iss is not None else None


def _store_pr_states() -> dict[int, str]:
    """PR number -> state (open/merged/closed) for every PR in the cockpit's PR
    store. The store keeps merged/closed PRs it once saw open, so most resolve here;
    a candidate PR is absent only when it merged/closed before any ingest captured
    it open, and its /api/prs/{n} detail would then 404. The one seam tests stub to
    stay off the real PR snapshot."""
    from app.backend import data
    return {n: pr.state for n, pr in data.prs().items() if pr.state}


_STATE_TTL = 300.0
_STATE_BATCH = 100
_pr_state_cache: dict[int, tuple[float, str]] = {}


def _live_pr_states(numbers: list[int]) -> dict[int, str]:
    """open/merged/closed for each PR via batched GraphQL reads (operator gh login —
    a read), TTL-cached in-process. Resolves the real state of candidate PRs the store
    doesn't have (they resolved before ingest saw them open), so the Issues view can
    label a merged fixer as merged instead of guessing. Chunked so a single bad number
    or a node-limit error fails only its own batch, not every PR on the tab; a PR that
    can't be resolved is simply omitted."""
    now = time.time()
    want = sorted({n for n in numbers
                   if n not in _pr_state_cache or now - _pr_state_cache[n][0] >= _STATE_TTL})
    owner, name = REPO.split("/")
    for i in range(0, len(want), _STATE_BATCH):
        batch = want[i:i + _STATE_BATCH]
        fields = " ".join(f"p{n}: pullRequest(number: {n}) {{ state }}" for n in batch)
        query = f'query {{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}'
        repo: dict = {}
        try:
            res = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}"],
                                 capture_output=True, text=True, timeout=20)
            if res.returncode == 0:
                repo = (json.loads(res.stdout).get("data") or {}).get("repository") or {}
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            repo = {}
        for n in batch:
            node = repo.get(f"p{n}")
            if node and node.get("state"):
                _pr_state_cache[n] = (now, node["state"].lower())
    return {n: _pr_state_cache[n][1] for n in numbers if n in _pr_state_cache}


# Explicit Fixes/Closes candidates lead — the PR's own claim it fixes the issue;
# fix-found candidates (a merged fixer the already-fixed detector attributed by
# symptom) follow; issue-ref candidates (the issue's text naming the PR) next;
# subsystem tag-matches last. Within each kind, a merged PR (likely resolved)
# first, a closed (abandoned) one next, open/unknown last. Ties break by PR
# number.
_STATE_RANK = {"merged": 0, "closed": 1}
_HOW_RANK = {"explicit": 0, "fix-found": 1, "issue-ref": 2}


def _how_rank(cand: dict) -> int:
    return _HOW_RANK.get(cand.get("how") or "", 3)


def _referenced(cand: dict) -> bool:
    """An evidence-backed candidate — explicit, detector-found, or issue-ref —
    never a tag-match."""
    return cand.get("how") in ("explicit", "fix-found", "issue-ref")


def _cluster_linked_prs(members: list[int], issues: dict[int, Issue],
                        store_states: dict[int, str],
                        live_states: dict[int, str] | None = None) -> list[dict]:
    """Union of candidate PRs across every issue in the cluster, deduped by PR
    number, each stamped with its real `state` (open/merged/closed) and `in_store`.
    Explicit Fixes/Closes matches lead, then issue-ref matches, then subsystem
    matches, each kind most-resolved first. A subsystem match landing on a
    duplicate (not the canonical) is the common case, so a cluster's fixing PRs
    must be gathered across all members, not read off the canonical alone. The
    strongest evidence kind wins when members link the same PR differently."""
    by_pr: dict[int, dict] = {}
    for n in members:
        iss = issues.get(n)
        if not iss:
            continue
        for cand in iss.candidate_prs:
            pr = cand.get("pr")
            if pr is None:
                continue
            existing = by_pr.get(pr)
            if existing is None or _how_rank(cand) < _how_rank(existing):
                by_pr[pr] = cand
    terminal = {"merged", "closed"}
    # A terminal store state (merged/closed) is authoritative and needs no live
    # fetch; any other PR is resolved live, since a store snapshot marked 'open'
    # can lag an upstream merge.
    live = (live_states if live_states is not None
            else _live_pr_states([pr for pr in by_pr if store_states.get(pr) not in terminal]))

    def _state(pr: int) -> str | None:
        stored = store_states.get(pr)
        return stored if stored in terminal else (live.get(pr) or stored)

    out = [{**by_pr[pr], "in_store": pr in store_states, "state": _state(pr)} for pr in by_pr]
    return sorted(out, key=lambda c: (_how_rank(c), _STATE_RANK.get(c["state"], 2), c["pr"]))


def _issue_linked_prs(iss: Issue, store_states: dict[int, str] | None,
                      *, limit: int | None = None) -> list[dict]:
    """The issue's candidate PRs — explicit Fixes/Closes matches first, then
    issue-ref matches, then subsystem tag-matches, each kind most-resolved first
    (merged, closed, then open/unknown) when store states are available. Trimming
    to `limit` happens after the sort, so a trim never drops the strongest fix
    evidence."""
    if limit == 0:
        return []
    if store_states is None:
        return sorted((dict(c) for c in iss.candidate_prs), key=_how_rank)[:limit]
    stamped = [{**c, "in_store": c["pr"] in store_states,
                "state": store_states.get(c["pr"])} for c in iss.candidate_prs]
    stamped.sort(key=lambda c: (_how_rank(c), _STATE_RANK.get(c["state"], 2), c.get("pr") or 0))
    return stamped[:limit]


def _row(iss: Issue, clusters_by_id: dict[int, IssueCluster], store_states: dict[int, str] | None,
         *, link_limit: int | None = None) -> dict:
    cid = iss.cluster_id
    cl = clusters_by_id.get(cid) if cid is not None else None
    members = cl.members if cl else [iss.number]
    canonical = cl.canonical if cl else None
    return {
        "number": iss.number,
        "title": iss.title,
        "author": iss.author,
        "trusted_author": iss.author in profile.active().trusted_authors,
        "labels": iss.labels,
        "comments": iss.comments,
        "reactions": iss.reactions_total,
        "thumbs_up": iss.thumbs_up,
        "created_at": iss.created_at,
        "updated_at": iss.updated_at,
        "state": iss.state,
        "url": iss.url or f"https://github.com/{REPO}/issues/{iss.number}",
        "subsystem": iss.subsystem or (cl.subsystem if cl else None),
        "repro_grade": iss.repro_grade,
        "repro_score": iss.repro_score,
        "pain": cl.pain if cl else None,
        "cluster": cid,
        "cluster_size": len(members),
        "canonical": canonical,
        "is_dup": bool(canonical and iss.number != canonical and len(members) > 1),
        "duplicates": [m for m in members if m != iss.number],
        "disposition": iss.disposition,
        "linked_prs": _issue_linked_prs(iss, store_states, limit=link_limit),
        "linked_pr_count": len(iss.candidate_prs),
        "referenced_pr_count": sum(1 for c in iss.candidate_prs if _referenced(c)),
        # Merged reference-backed fixers — the "likely fixed" signal the prs sort
        # leads with.
        "referenced_merged_count": 0 if store_states is None else sum(
            1 for c in iss.candidate_prs
            if _referenced(c) and store_states.get(c["pr"]) == "merged"),
    }


def list_issues() -> list[dict]:
    """Every issue in the store, enriched with its dedup cluster, pain, repro
    grade, and the PRs that may address it."""
    _sync_store_root()
    issues = issue_data.full_issues()
    clusters = issue_data.clusters()
    store_states = _store_pr_states()
    return [_row(i, clusters, store_states) for i in issues.values()]


def get_issue(n: int) -> dict | None:
    """One issue's detail: its table row plus body, the full analysis section
    (disposition, gist, rationale, asks, canonical), and its cluster's curated
    label — what the issue flyout renders."""
    _sync_store_root()
    issues = issue_data.full_issues()
    i = issues.get(int(n))
    if i is None:
        return None
    clusters = issue_data.clusters()
    row = _row(i, clusters, _store_pr_states())
    row["body"] = i.body
    a = i.rec.get("analysis")
    row["analysis"] = a
    a = a or {}
    fixer = a.get("fixed_by")
    canon = a.get("canonical")
    row["fixed_comment"] = fixed_issue_comment(int(fixer)) if fixer else None
    row["dup_comment"] = dup_issue_comment(int(canon)) if canon else None
    cl = clusters.get(i.cluster_id) if i.cluster_id else None
    row["cluster_label"] = (cl.curation or {}).get("label") if cl else None
    return row


_REPRO_RANK = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1, "F": 0}
# Most actionable first; unanalyzed issues have no rank and always sort last.
_DISP_RANK = {"link-pr": 0, "close-dup": 1, "request-repro": 2, "needs-human": 3}
_ISSUE_SORT_KEYS = {
    "number": lambda r: r["number"],
    "title": lambda r: (r["title"] or "").lower(),
    "author": lambda r: (r["author"] or "").lower(),
    "pain": lambda r: r["pain"],
    "repro": lambda r: _REPRO_RANK.get(r["repro_grade"] or ""),
    "dups": lambda r: len(r["duplicates"]),
    # Fix evidence first: merged referenced fixers, then referenced PRs, then volume.
    "prs": lambda r: (r["referenced_merged_count"], r["referenced_pr_count"], r["linked_pr_count"]),
    "disposition": lambda r: _DISP_RANK.get(r["disposition"] or ""),
    "subsystem": lambda r: (r["subsystem"] or "").lower(),
}
_ISSUE_DEFAULT_DESC = {"number", "pain", "repro", "dups", "prs"}


def _sort_rows(rows: list[dict], sort: str | None, direction: str | None) -> None:
    key = _ISSUE_SORT_KEYS.get(sort or "", _ISSUE_SORT_KEYS["pain"])
    reverse = direction == "desc" if direction in ("asc", "desc") else (sort or "pain") in _ISSUE_DEFAULT_DESC
    present = [r for r in rows if key(r) is not None]
    missing = [r for r in rows if key(r) is None]
    present.sort(key=lambda r: (key(r), r["number"]), reverse=reverse)
    missing.sort(key=lambda r: r["number"])
    rows[:] = present + missing


def query_issues(q: str = "", sort: str | None = None, direction: str | None = None,
                 disposition: str | None = None, state: str | None = None,
                 author: str | None = None, pain: dict | None = None,
                 repro_grade: str | list[str] | None = None,
                 subsystem: str | None = None,
                 dups: dict | None = None, linked_prs: dict | None = None,
                 labels: str | None = None,
                 offset: int = 0, limit: int = 50) -> dict:
    """Paginated issue-table query. Uses the light cached snapshot for filtering
    and sorting, then hydrates only the returned page with full candidate PR data.
    Sorting by linked PRs, or filtering by `linked_prs`, needs every issue's
    candidate links (sorting also needs store PR states, to rank merged fixers),
    so either builds from the full snapshot instead.
    `disposition` filters to one triage disposition; "none" selects unanalyzed
    issues. `state` filters by GitHub lifecycle state ("open"/"closed"); "all" or
    None returns both. `author` is a case-insensitive starts-with match; `subsystem`
    and `labels` are case-insensitive substring matches (`labels` against any of
    the issue's labels); `repro_grade` accepts one value or a list (OR'd); `pain`,
    `dups` (duplicate count), and `linked_prs` (linked-PR count) are `{op, value}`
    numeric compares — see filters.num_cmp — and a missing row value never matches
    one."""
    _sync_store_root()
    need_full = sort == "prs" or linked_prs is not None
    issues = issue_data.full_issues() if need_full else issue_data.issues()
    clusters = issue_data.clusters()
    sort_states = _store_pr_states() if sort == "prs" else None
    rows = [_row(i, clusters, sort_states, link_limit=0) for i in issues.values()]
    if disposition:
        rows = [r for r in rows if (r["disposition"] or "none") == disposition]
    if state and state != "all":
        rows = [r for r in rows if r["state"] == state]
    if author:
        needle_author = author.strip().lower()
        if needle_author:
            rows = [r for r in rows if (r["author"] or "").lower().startswith(needle_author)]
    if pain is not None:
        rows = [r for r in rows if num_cmp(r["pain"], pain)]
    if repro_grade:
        wanted = repro_grade if isinstance(repro_grade, list) else [repro_grade]
        rows = [r for r in rows if r["repro_grade"] in wanted]
    if subsystem:
        needle_subsystem = subsystem.strip().lower()
        if needle_subsystem:
            rows = [r for r in rows if needle_subsystem in (r["subsystem"] or "").lower()]
    if dups is not None:
        rows = [r for r in rows if num_cmp(len(r["duplicates"]), dups)]
    if linked_prs is not None:
        rows = [r for r in rows if num_cmp(r["linked_pr_count"], linked_prs)]
    if labels:
        needle_label = labels.strip().lower()
        if needle_label:
            rows = [r for r in rows if any(needle_label in (lbl or "").lower() for lbl in r["labels"])]
    needle = q.strip().lower()
    if needle:
        rows = [
            r for r in rows
            if needle == str(r["number"])
            or needle in (r["title"] or "").lower()
            or needle in (r["author"] or "").lower()
            or needle in (r["subsystem"] or "").lower()
        ]
    _sort_rows(rows, sort, direction)
    total = len(rows)
    page = rows[offset:offset + limit]
    full = issue_data.load_full_issues([r["number"] for r in page])
    store_states = _store_pr_states()
    hydrated = []
    for r in page:
        iss = full.get(r["number"])
        hydrated.append(_row(iss, clusters, store_states, link_limit=6) if iss else r)
    return {"items": hydrated, "total": total, "offset": offset, "limit": limit}


_dup_groups_cache: tuple[tuple[str | None, str | None], list[dict]] | None = None


def duplicate_groups() -> list[dict]:
    """The curated close-as-dup worklist, grouped by canonical issue: each group is
    a set of confirmed duplicate issues to close against one canonical, with the
    candidate PRs from every member cross-linked. Most painful first (#192)."""
    global _dup_groups_cache
    _sync_store_root()
    key = issue_data.watermarks()
    if _dup_groups_cache is not None and _dup_groups_cache[0] == key:
        return _dup_groups_cache[1]
    issues = issue_data.full_issues()
    clusters = issue_data.clusters()
    from issue_triage import issue_gates
    store_states = _store_pr_states()
    pending: list[tuple[IssueCluster, list[dict], Issue | None]] = []
    for cl in clusters.values():
        canon = cl.canonical
        if canon is None:
            continue
        dups = []
        for n in cl.members:
            iss = issues.get(n)
            if not iss or n == canon:
                continue
            ok, _ = issue_gates.close_dup_allowed(iss, cl)
            if ok:
                dups.append({"number": n, "title": iss.title, "author": iss.author,
                             "trusted_author": iss.author in profile.active().trusted_authors,
                             "repro_grade": iss.repro_grade,
                             "url": f"https://github.com/{REPO}/issues/{n}"})
        if not dups:
            continue
        canon_iss = issues.get(canon)
        pending.append((cl, dups, canon_iss))

    terminal = {"merged", "closed"}
    pr_numbers = {
        cand["pr"]
        for cl, _, _ in pending
        for n in cl.members if issues.get(n)
        for cand in issues[n].candidate_prs
        if cand.get("pr") is not None and store_states.get(cand["pr"]) not in terminal
    }
    live_states = _live_pr_states(sorted(pr_numbers))
    groups: list[dict] = []
    for cl, dups, canon_iss in pending:
        canon = cl.canonical
        linked = _cluster_linked_prs(cl.members, issues, store_states, live_states)
        # The merged PR that fixes a cluster member — explicit or detector-found,
        # the two kinds close_fixed_gate accepts — offered as the card's "close as
        # fixed" fixer. `linked` is rank-sorted, so an explicit fixer wins.
        fixer = next((lp["pr"] for lp in linked
                      if lp.get("how") in ("explicit", "fix-found")
                      and lp.get("state") == "merged"), None)
        groups.append({
            "canonical": canon,
            "canonical_title": canon_iss.title if canon_iss else None,
            "canonical_url": f"https://github.com/{REPO}/issues/{canon}",
            "cluster": cl.id,
            "label": (cl.curation or {}).get("label"),
            "pain": cl.pain,
            "subsystem": cl.subsystem,
            "linked_prs": linked,
            "dups": dups,
            # The exact default notes the executor would post, so the card prefills
            # the editable box with them rather than paraphrasing in a placeholder.
            "dup_comment": dup_issue_comment(canon),
            "fixed_comment": fixed_issue_comment(fixer) if fixer is not None else None,
        })
    groups = sorted(groups, key=lambda g: -(g.get("pain") or 0))
    _dup_groups_cache = (key, groups)
    return groups


def already_fixed() -> dict[str, list[dict]]:
    """The already-fixed worklist. `fixed` = tier-1 open issues with a close-fixed
    disposition, a current fix_scan, and a fixer that a live check shows merged —
    each prefilled with the bot comment, pain-sorted. `likely_fixed` = tier-2 open
    issues (fix_scan.status == "likely-fixed") for human review. `not-fixed` issues
    appear in neither."""
    _sync_store_root()
    issues = issue_data.full_issues()
    clusters = issue_data.clusters()
    from issue_triage.issue_freshness import is_current

    def pain_of(iss: Issue) -> float:
        cl = clusters.get(iss.cluster_id) if iss.cluster_id else None
        return (cl.pain if cl else 0.0) or 0.0

    fixed_candidates: list[tuple[int, int, dict]] = []
    likely: list[dict] = []
    for n, iss in issues.items():
        if iss.state != "open":
            continue
        scan = iss.fix_scan or {}
        status = scan.get("status")
        if status == "fixed" and iss.disposition == "close-fixed" \
                and is_current(iss, "fix_scan") and iss.fixed_by is not None:
            fixed_candidates.append((n, int(iss.fixed_by), scan))
        elif status == "likely-fixed" and is_current(iss, "fix_scan"):
            likely.append({"number": n, "title": iss.title,
                           "gist": scan.get("gist"), "rationale": scan.get("rationale"),
                           "pain": pain_of(iss),
                           "url": f"https://github.com/{REPO}/issues/{n}"})
    fixer_states = _live_pr_states(sorted({pr for _, pr, _ in fixed_candidates}))
    fixed: list[dict] = []
    for n, pr, scan in fixed_candidates:
        if fixer_states.get(pr) != "merged":
            continue
        iss = issues[n]
        fixed.append({"number": n, "title": iss.title, "fixed_by": pr,
                      "gist": scan.get("gist"), "rationale": scan.get("rationale"),
                      "upstream_date": scan.get("upstream_date"),
                      "pain": pain_of(iss),
                      "comment": fixed_issue_comment(pr),
                      "url": f"https://github.com/{REPO}/issues/{n}",
                      "fixer_url": f"https://github.com/{REPO}/pull/{pr}"})
    fixed.sort(key=lambda g: -(g.get("pain") or 0))
    likely.sort(key=lambda g: -(g.get("pain") or 0))
    return {"fixed": fixed, "likely_fixed": likely}


def _live_state(n: int) -> str | None:
    """Issue `n`'s current upstream state ("open"/"closed") fetched live from
    GitHub, or None when the fetch fails."""
    r = run(["gh", "api", f"repos/{REPO}/issues/{n}", "--jq", ".state"], timeout=30)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _live_state_reason(n: int) -> str | None:
    """Issue `n`'s current upstream close reason, or None when it is open or the
    fetch fails. GitHub reports the app's FIXED disposition as ``completed``."""
    r = run(["gh", "api", f"repos/{REPO}/issues/{n}", "--jq",
             ".state_reason // empty"], timeout=30)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def close_fixed_gate(n: int, fixed_by: int) -> tuple[bool, str]:
    """The executor's pre-write gate for closing issue `n` as fixed by PR `fixed_by`.
    `fixed_by` must be a recorded fix candidate — an explicit Fixes/Closes/Resolves
    reference or a detector-found (`fix-found`) fixer — of the issue or one of its
    cluster siblings — the same fixer link the card surfaces — so neither an
    arbitrary merged PR nor a mere subsystem tag-match can close an
    issue; and it must be currently merged, re-verified live at write time (the
    render-warmed cache entry is evicted first, never trusting a stored snapshot —
    the staleness that motivates this path). The issue's own state is resolved live
    too, so an already-closed issue is blocked at this gate. See
    issue_gates.close_fixed_eligibility."""
    st = _store()
    issues = st.all_issues()
    iss = issues.get(int(n))
    if iss is None:
        return False, "issue not in store"
    members = [int(n)]
    if iss.cluster_id:
        cl = st.load_issue_cluster(iss.cluster_id)
        if cl:
            members = cl.members
    candidates = {c.get("pr") for m in members if issues.get(m)
                  for c in issues[m].candidate_prs if c.get("how") in ("explicit", "fix-found")}
    if int(fixed_by) not in candidates:
        return False, f"#{fixed_by} is not a fix candidate of issue #{n}"
    from issue_triage import issue_gates
    _pr_state_cache.pop(int(fixed_by), None)  # the write-time merge check must be live, not render-warmed
    state = _live_pr_states([int(fixed_by)]).get(int(fixed_by))
    return issue_gates.close_fixed_eligibility(iss, state, _live_state(int(n)))


def fixed_issue_comment(pr: int) -> str:
    """The comment the configured bot posts when closing an issue as fixed by a merged
    PR — points the reporter at the PR that resolved it."""
    return (f"Thanks for filing this! This looks resolved by #{pr}, which has merged. Closing as "
            "fixed — please reopen if you still hit it on a build that includes that change.")


def close_dup_gate(n: int) -> tuple[bool, str]:
    """The executor's pre-write gate for closing issue `n` as a duplicate — the
    issue-side analog of gates.merge_eligibility. Loads the issue + its cluster
    from the store and applies issue_gates.close_dup_eligibility, live-checking
    upstream that the duplicate is open and its canonical is open or was closed
    as completed/fixed. Other closed canonicals remain blocked."""
    st = _store()
    issues = st.all_issues()
    iss = issues.get(int(n))
    if iss is None:
        return False, "issue not in store"
    from issue_triage import issue_gates
    cl = st.load_issue_cluster(iss.cluster_id) if iss.cluster_id else None
    return issue_gates.close_dup_eligibility(
        iss, cl, issues, live_state=_live_state, live_state_reason=_live_state_reason)


def close_gate(n: int, disposition: str, comment: str | None,
               fixed_by: int | None, canonical: int | None) -> tuple[bool, str]:
    """The executor's pre-write gate for an operator-directed issue close.
    `disposition` is one of 'not-planned' | 'completed' | 'fixed' | 'dup'. Every
    disposition requires the issue in the store and still open upstream (live-checked
    via _live_state, fail-open when GitHub is unreachable). A plain close
    (not-planned/completed) also requires a non-empty comment. 'fixed' requires a
    `fixed_by` PR that a live check does not show unmerged; 'dup' requires a
    `canonical` issue that is open or closed as completed/fixed. Operator-asserted:
    it does not require the fixer/dup link to be one the pipeline already recorded."""
    if disposition not in ("not-planned", "completed", "fixed", "dup"):
        return False, f"invalid disposition {disposition!r}"
    if disposition == "fixed" and not fixed_by:
        return False, "a fixer PR is required"
    if disposition == "dup" and not canonical:
        return False, "a canonical issue is required"
    if disposition in ("not-planned", "completed") and not (comment or "").strip():
        return False, "a comment is required"
    st = _store()
    if st.all_issues().get(int(n)) is None:
        return False, "issue not in store"
    if _live_state(int(n)) == "closed":
        return False, "issue already closed"
    if disposition == "fixed" and fixed_by is not None:
        _pr_state_cache.pop(int(fixed_by), None)  # the merged check must be live, not render-warmed
        state = _live_pr_states([int(fixed_by)]).get(int(fixed_by))
        if state is not None and state != "merged":
            return False, f"#{fixed_by} is not merged"
    elif disposition == "dup" and canonical is not None and _live_state(int(canonical)) == "closed":
        reason = _live_state_reason(int(canonical))
        if reason != "completed":
            detail = f" ({reason})" if reason else ""
            return False, f"canonical #{canonical} is closed{detail}"
    return True, "ok"


def dup_issue_comment(canonical: int | None) -> str:
    """The comment the configured bot posts when closing an issue as a duplicate —
    points the reporter at the canonical issue so the thread isn't lost (#192)."""
    if canonical:
        return (f"Thanks for filing this! It looks like a duplicate of #{canonical}, which tracks the same "
                "problem. Closing this in favor of that one — please follow along there, and add anything new "
                "(repro steps, environment, logs) as a comment on it if it helps.")
    return "Thanks for filing this! Closing as a duplicate during triage — please reopen if that's not right."
