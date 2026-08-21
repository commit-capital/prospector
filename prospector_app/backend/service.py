"""Join layer: shape pipeline-store records for the SPA.

All policy comes from pipeline/gates.py; all freshness from pipeline/freshness.py.
This module only flattens, filters, sorts. Diff fetching shells out through
safety_guard (gh pr diff) and caches to prospector_app/cache/diffs/<sha>.diff.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.model import Pr

from prospector_app.backend import data
from prospector_app.backend import filters  # spec predicate
from prospector_app.backend import pr_checks
from prospector_app.backend import responses
from prospector_app.backend import suggest
from prospector_app.backend import testpaths
from prospector_app.backend import verify_view
from prospector_app.backend.safety_guard import run

from pipeline import settings
from pipeline import codeowners  # pipeline (path set up by data import)
from pipeline import gh
from pipeline import reviewers
from pipeline import freshness
from pipeline import gates
from pipeline import profile
from pipeline import risktier

CACHE_DIR = Path(__file__).resolve().parents[1] / "cache"
DIFF_CACHE = CACHE_DIR / "diffs"
# The pipeline caches every PR's diff during clustering/threat-scan; it's far
# broader than the app's own lazily-fetched cache, so read it as a fallback
# (read-only, no network) — this is what gives changed_paths / size_split broad
# coverage for the list view.
PIPELINE_DIFF_CACHE = Path(__file__).resolve().parents[2] / "pipeline" / "cache" / "diffs"


def diff_text(rec: Pr) -> str | None:
    """A PR's cached unified diff: the app's own cache first, then the
    pipeline's broad cache. None when neither has it."""
    return (testpaths.cached_diff_text(rec, DIFF_CACHE)
            or testpaths.cached_diff_text(rec, PIPELINE_DIFF_CACHE))


# Diff-derived facets (test/non-test size split + changed file paths) are parsed
# from each PR's cached diff — too expensive to redo for the whole queue on every
# list query, so memoize by head SHA. A moved head yields a new SHA and a fresh
# parse; misses aren't cached, so a diff that gets cached later is still picked up.
_FACETS: dict[str, dict] = {}


def _facets(rec: Pr) -> dict:
    sha = rec.head_sha or ""
    if not sha:
        return {}
    hit = _FACETS.get(sha)
    if hit is not None:
        return hit
    text = diff_text(rec)
    if not text:
        return {}
    hit = testpaths.diff_facets(text)
    # path-based risk tier rides the same memo: classifying every path against
    # the glob map is too slow to redo per list query
    hit["risk"] = risktier.tier_facet(hit["changed_paths"])
    _FACETS[sha] = hit
    return hit


def changed_paths(rec: Pr) -> list[str]:
    """The PR's changed file paths from its cached diff, memoized by head SHA
    (the _facets memo); [] when no diff is cached."""
    return _facets(rec).get("changed_paths") or []


# ---------------------------------------------------------------------------
# PR rows.
# ---------------------------------------------------------------------------
def _signal_summary(sig: dict | None, rec: Pr) -> dict | None:
    """The row's compact signals. The Greptile fields project the Greptile
    reviewer entry (score, whether it predates the head, and the semantic read's
    severity gated on that read's freshness) — they are the Explorer's Greptile
    score/freshness/severity filters and search vocabulary."""
    if not sig and rec.review_entry("greptile") is None:
        return None
    sig = sig or {}
    ds = sig.get("diffstat") or {}
    return {
        "greptile": rec.greptile,
        "greptile_stale": rec.greptile_stale,
        "greptile_severity": (rec.greptile_severity
                              if freshness.is_current(rec, "greptile_review") else None),
        "ci": sig.get("ci"),
        "conflicts": not sig.get("mergeable", True),
        "has_tests": sig.get("has_tests"),
        "additions": ds.get("additions"),
        "deletions": ds.get("deletions"),
        "changed_files": ds.get("changed_files"),
    }


def _summary_line(rec: Pr) -> dict | None:
    """Trimmed summary projection for list rows — just the headline and primary
    change, so a 50-row page doesn't carry the full mechanism/paths arrays. The
    detail view returns the whole summary section instead."""
    s = rec.section("summary")
    if not s:
        return None
    return {"one_liner": s.get("one_liner"), "primary_change": s.get("primary_change")}


def _human_merge_from_cache(rec: Pr) -> dict | None:
    """CODEOWNERS manual-merge requirement derived from the cached diff (no
    network). None when no gated path — or when the diff isn't cached yet."""
    paths = _facets(rec).get("changed_paths")
    if paths is None:
        return None
    return codeowners.human_merge(paths)


def _human_merge_cached(rec: Pr) -> tuple[bool, dict | None]:
    """`(known, value)` for the CODEOWNERS check from the cached diff.

    `known` is True only when the diff is actually cached — then `value` is
    authoritative (`None` genuinely means "no gated path"). When the diff isn't
    cached we return `(False, None)`: a live file list is required, because a
    bare `None` can't be told apart from "not required" and trusting it would
    hide the human-merge gate on an uncached infra PR."""
    text = testpaths.cached_diff_text(rec, DIFF_CACHE)
    if text is None:
        return False, None
    return True, codeowners.human_merge(testpaths.changed_paths(text))


def live_changed_paths(n: int) -> list[str] | None:
    """The PR's changed-file list from GitHub — authoritative for the
    CODEOWNERS check and the risk tier when no diff is cached. None when the
    fetch fails — never asserted as "no paths changed", so a caller can tell
    a failed fetch apart from a PR that genuinely touches nothing gated."""
    r = run(["gh", "api", "--paginate", f"repos/{settings.repo()}/pulls/{n}/files",
             "--jq", ".[].filename"], timeout=60)
    if r.returncode != 0:
        return None
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _pr_body_live(n: int) -> str | None:
    """Fetch a PR body from GitHub — the lazy fallback when it wasn't ingested."""
    res = run(["gh", "api", f"repos/{settings.repo()}/pulls/{n}", "--jq", ".body"], timeout=30)
    return res.stdout.strip() if res.returncode == 0 else None


def _ci_checks(head_sha: str | None) -> list[dict]:
    """The PR head's check runs, read from GitHub."""
    return gh.check_runs(head_sha or "")


def reviews_detail(n: int) -> dict[str, dict]:
    """Every reviewer's stored entry and digest on PR `n`, keyed by reviewer id —
    the PR page's per-reviewer blocks."""
    rec = data.prs().get(int(n))
    if rec is None:
        return {}
    return {rid: {"entry": rec.review_entry(rid), "digest": d}
            for rid, d in reviewers.digests(rec).items()}


def _resolve_live(fut, fallback, failed: list[str], label: str):
    """Resolve one fanned-out live future, degrading to `fallback` (and recording
    `label` in `failed`) when its round-trip raised. A `gh` timeout on a single
    live call must not propagate out and 500 the whole detail view — the cached
    store data still renders."""
    try:
        return fut.result()
    except Exception:
        failed.append(label)
        return fallback


def suggestion_for(n: int, disposition: str | None = None) -> dict | None:
    """The suggestion card for a PR, optionally recomputed as if its disposition
    were `disposition` — keeps the bot comment in sync with the operator's
    selected action (#13)."""
    rec = data.prs().get(int(n))
    if rec is None:
        return None
    return suggest.suggest_for_record(rec, disposition=disposition,
                                      human_merge=_human_merge_from_cache(rec))


def _age_days(pr: Pr) -> int | None:
    ts = pr.updated_at or pr.created_at
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - d).days
    except (ValueError, TypeError):
        return None


def _fact_freshness(rec: Pr) -> list[dict]:
    """Per-fact provenance for every sha-bound section the PR carries: when it was
    computed, which head it describes, whether it still holds, and why not.

    This is the answer to "when is this recommendation from, and does it still
    apply?" — the question a rationale alone cannot settle."""
    out: list[dict] = []
    for section in freshness.SHA_BOUND:
        sec = rec.section(section)
        if not sec:
            continue
        max_age = gates.SECURITY_MAX_AGE_DAYS if section == "security" else None
        out.append({
            "section": section,
            "checked_at": sec.get("checked_at"),
            "against_head_sha": sec.get("against_head_sha"),
            "current": freshness.is_current(rec, section, max_age_days=max_age),
            "why": freshness.currency_failure(rec, section, max_age_days=max_age),
        })
    return out


def _merge_gate_fields(rec: Pr) -> dict:
    """The merge gate for the app: ok/reason from gates.merge_eligibility,
    plus `overridable` (a reason at merge time clears the block) and
    `override_kind` naming which block ("security" for a YELLOW verdict,
    "verify" for an escalate outcome) so the UI logs and labels the override to
    the right section. The two are mutually exclusive by construction."""
    ok, reason = gates.merge_eligibility(rec)
    sec = gates.security_overridable(rec)
    ver = gates.verify_overridable(rec)
    return {"ok": ok, "reason": reason, "overridable": sec or ver,
            "override_kind": "security" if sec else "verify" if ver else None}


def _row_merge_gate(rec: Pr) -> dict:
    """Store-only human-merge verdict for list rows (no live CODEOWNERS round-trip;
    pr_detail layers the authoritative live check on top)."""
    gate = _merge_gate_fields(rec)
    hm = _human_merge_from_cache(rec)
    if hm and hm.get("required"):
        gate["ok"], gate["overridable"], gate["override_kind"] = False, False, None
        gate["reason"] = f"requires manual merge by a code owner ({' '.join(hm.get('owners', []))})"
    return gate


def pr_row(n: int, rec: Pr | None = None) -> dict | None:
    rec = rec or data.prs().get(int(n))
    if rec is None:
        return None
    clean, clean_reasons = gates.pr_clean(rec)
    hm = _human_merge_from_cache(rec)
    sug = suggest.suggest_for_record(rec, human_merge=hm)
    facets = _facets(rec)
    pain = pr_pain(rec)
    return {
        "number": rec.number,
        "title": rec.title,
        "author": rec.author,
        "head_sha": rec.head_sha,
        "live_head_sha": rec.live_head_sha,
        "fact_freshness": _fact_freshness(rec),
        "url": rec.url,
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
        "draft": rec.draft,
        "github_state": rec.state,
        "clusters": data.pr_to_clusters().get(rec.number, []),
        "disposition": rec.disposition,
        "proposed_action": {
            "action": rec.disposition,
            "canonical": rec.canonical,
            "upstream_pr": rec.upstream_pr,
            "upstream_commit": rec.upstream_commit,
            "upstream_date": rec.upstream_date,
            "asks": rec.asks,
            "rationale": rec.rationale,
            "fresh": freshness.is_current(rec, "analysis"),
        },
        "safety": rec.security_verdict,
        "safety_fresh": freshness.is_current(rec, "security",
                                             max_age_days=gates.SECURITY_MAX_AGE_DAYS),
        "safety_findings": len(rec.findings),
        "safety_titles": [
            {"severity": f.get("severity"), "title": f.get("title"), "location": f.get("location")}
            for f in rec.findings
        ],
        "drift_state": rec.drift_state,
        "threat": rec.threat_verdict,
        "clean": clean,
        "clean_reasons": clean_reasons,
        "age_days": _age_days(rec),
        "merge_gate": _row_merge_gate(rec),
        "stale_sections": freshness.stale_sections(rec),
        "issues": rec.linked_issues,
        "trusted_author": rec.author in profile.active().trusted_authors,
        "signals": _signal_summary(rec.signals, rec),
        "reviews": reviewers.digests(rec),
        "author_stats": data.author_stats(rec.author),
        "summary": _summary_line(rec),
        # test/non-test LOC split + effective-LOC breakdown + changed file list,
        # all from one cached-diff parse (None/[] until the diff is cached — the
        # UI falls back to the aggregate size); see #17/#22. changed_paths backs
        # the `paths` filter; loc_breakdown strips generated artifacts so the LOC
        # column shows the lines a human actually wrote.
        "size_split": facets.get("size_split"),
        "loc_breakdown": facets.get("loc_breakdown"),
        "changed_paths": facets.get("changed_paths") or [],
        # path-based blast-radius tier (pipeline/risktier.py): 0 = core/supply
        # chain … 3 = leaf; None until the diff is cached. An ordering/attention
        # signal only — never a gate input.
        "risk_tier": (facets.get("risk") or {}).get("tier"),
        # CODEOWNERS manual-merge requirement from the cached diff (#15/#26);
        # pr_detail recomputes this from a live file list (authoritative).
        "human_merge": hm,
        "checks": pr_checks.checks_for_record(rec),
        "suggestion": sug,
        # Deferred out of triage (dependency bump). The app shows a single
        # "handled upstream" banner instead of the merge/security/action surface.
        "out_of_scope": sug.get("action") == "OUT_OF_SCOPE",
        # how the community responded to our triage since we acted (#community-signals)
        "responses": responses.for_pr(rec.number),
        "pain_score": pain["score"],
        "pain_breakdown": {
            "issue_pain": pain["issue_pain"],
            "linked_issues": pain["linked_issues"],
            "pr_comments": pain["pr_comments"],
            "pr_reactions": pain["pr_reactions"],
        },
    }


_SAFETY_RANK = {"RED": 3, "YELLOW": 2, "GREEN": 1, None: 0}

def _loc_total(r: dict) -> int:
    d = r["signals"] or {}
    return (d.get("additions") or 0) + (d.get("deletions") or 0)


def _loc_effective(r: dict) -> int:
    """Sort key for LOC: effective lines (diff minus generated artifacts) when the
    breakdown is available, else the raw aggregate. Sorting desc then surfaces PRs
    that are genuinely large, not ones bloated by regenerated snapshots."""
    b = r.get("loc_breakdown")
    return b["effective"] if b else _loc_total(r)


def _checks_ratio(r: dict) -> float:
    """Fraction of a PR's run checks that passed (−1 when nothing ran), so a
    Checks sort surfaces the most/least vetted PRs."""
    c = r.get("checks") or {}
    total = c.get("total") or 0
    return (c.get("passed") or 0) / total if total else -1.0


def _author_rate(r: dict) -> float:
    """The author's confidence-weighted merge rate (`merge_rate_shrunk`) — the sort
    key for the Explorer's 'Author rate' column. An author with no leaderboard row
    sorts last."""
    v = (r.get("author_stats") or {}).get("merge_rate_shrunk")
    return v if v is not None else -1.0


# Sort key per column the PR Explorer exposes. Every list column is sortable
# (#180); the engine sorts server-side so it spans all pages, not just the
# visible one. Keys that can be absent push their row to the end of an ascending
# sort via a leading `(is_missing, …)` tuple.
_SORT_KEYS = {
    "pr": lambda r: r["number"],
    "greptile": lambda r: (r["signals"] or {}).get("greptile") or -1,
    "safety": lambda r: _SAFETY_RANK.get(r["safety"], 0),
    "updated": lambda r: r["updated_at"] or "",
    "author": lambda r: (r["author"] or "").lower(),
    "loc": _loc_effective,
    "files": lambda r: (r["signals"] or {}).get("changed_files") or 0,
    "title": lambda r: (r["title"] or "").lower(),
    "cluster": lambda r: (not r["clusters"], r["clusters"][0] if r["clusters"] else 0),
    "disposition": lambda r: (r["disposition"] is None, r["disposition"] or ""),
    "checks": _checks_ratio,
    "drift": lambda r: (not r["drift_state"], r["drift_state"] or ""),
    "merge": lambda r: 1 if (r["merge_gate"] or {}).get("ok") else 0,
    "tier": lambda r: (r["risk_tier"] is None, r["risk_tier"] if r["risk_tier"] is not None else 0),
    "age": lambda r: r["age_days"] if r["age_days"] is not None else -1,
    "author_rate": _author_rate,
    "summary": lambda r: ((r["summary"] or {}).get("one_liner") or "").lower(),
    "pain": lambda r: r.get("pain_score") or 0,
    "issues": lambda r: sum(
        1 for issue in r.get("issues") or []
        if issue.get("how") in ("explicit", "fix-found", "issue-ref")),
}
_DEFAULT_DESC = {"pr", "greptile", "safety", "updated", "loc", "files",
                 "checks", "merge", "age", "author_rate", "pain", "issues"}


# A pr_row is a pure projection of its store record, the cluster index, the
# author table, and repo config — plus the current UTC date (age and staleness
# windows) — except `responses`, which the query loop overlays fresh below.
# Building one walks every gate and freshness window, so rebuilding the whole
# corpus per list query costs seconds at a few thousand PRs; rows are cached
# here instead, filled lazily per PR, and the cache is replaced wholesale when
# the snapshot (generation + object identity) or the date moves. The object
# identity guards callers that swap `data.prs` out from under the generation
# counter (tests); the generation guards id() reuse across snapshot swaps.
_ROW_CACHE: dict[int, dict] = {}
_ROW_CACHE_KEY: tuple[int, int, str] | None = None


def _row_cache(snap: dict[int, Pr]) -> dict[int, dict]:
    global _ROW_CACHE, _ROW_CACHE_KEY
    key = (data.generation(), id(snap), datetime.now(timezone.utc).date().isoformat())
    if _ROW_CACHE_KEY != key:
        # Rebind rather than clear: a query that started under the old key keeps
        # filling (and reading) the dict it already holds, coherently.
        _ROW_CACHE = {}
        _ROW_CACHE_KEY = key
    return _ROW_CACHE


def query_prs(spec: dict, sort: str | None = None, direction: str | None = None,
              offset: int = 0, limit: int = 50) -> dict:
    """The one PR list endpoint's engine. `spec` is a filter spec (see filters.py).
    Returns a page of rows plus match_ids (every matching PR number) so the UI can
    select-all across pages."""
    eff = dict(spec)
    snap = data.prs()
    cache = _row_cache(snap)
    rows = []
    state_spec = eff.get("state")
    for n, rec in snap.items():
        if state_spec == "all":
            pass
        elif state_spec == "closed":
            if rec.state == "open":
                continue
        # Default: the queue is open-PRs-only, but a community response (a
        # reply, a reopen) is often on a PR we already closed — when filtering
        # by response, widen to any state so that pushback still surfaces.
        elif rec.state != "open" and not eff.get("responses"):
            continue
        row = cache.get(n)
        if row is None:
            row = pr_row(n, rec)
            if row is None:
                continue
            cache[n] = row
        # Response signals and their acks live outside the snapshot (registry +
        # store, own short-TTL caches), so overlay them fresh on the cached row —
        # an ack must drop the PR from the responses queue on the very next query.
        row = {**row, "responses": responses.for_pr(n)}
        if filters.matches(row, eff):
            rows.append(row)
    key = _SORT_KEYS.get(sort or "", _SORT_KEYS["pr"])
    reverse = direction == "desc" if direction in ("asc", "desc") else (sort or "pr") in _DEFAULT_DESC
    rows.sort(key=key, reverse=reverse)
    return {"items": rows[offset:offset + limit], "total": len(rows),
            "offset": offset, "limit": limit,
            "match_ids": [r["number"] for r in rows]}


def count_prs(specs: list[dict]) -> list[int]:
    """Match totals for a batch of filter specs, one total per spec in order.
    Each spec is evaluated by query_prs, so a total always agrees with the
    Explorer's result set for the same spec."""
    return [query_prs(spec, limit=0)["total"] for spec in specs]


# ---------------------------------------------------------------------------
# Cluster board + detail.
# ---------------------------------------------------------------------------

# Known bot logins whose PR comments and reactions don't represent community pain.
# `[bot]`-suffixed logins are checked dynamically; this covers named service accounts.
_BOT_LOGINS: frozenset[str] = frozenset({settings.bot_login(), "github-actions"})


def _is_bot_author(login: str | None) -> bool:
    return not login or login in _BOT_LOGINS or login.endswith("[bot]")


def pr_pain(pr: Pr) -> dict:
    """Community Pain Score for a single PR.

    Three signals:
    - Linked issue pain scores (from issue_triage, summed) — counting only
      ``explicit`` links (author-declared ``Fixes #N``), not ``subsystem``
      matches. A subsystem match is a discovery hint ("this PR touches the same
      area"), not a fix-claim; summing it would falsely credit a PR with every
      issue in its subsystem and credit that pain identically to every other PR
      touching the area.
    - PR comment count (excluded when author is a bot)
    - PR reaction count (excluded when author is a bot)

    Returns a dict with ``score`` (the composite float) and breakdown fields.
    """
    fixed = [link for link in pr.linked_issues if link.get("how") == "explicit"]
    issue_pain = sum(float(link.get("pain") or 0) for link in fixed)
    if not _is_bot_author(pr.author):
        pr_comments = pr.comments or 0
        pr_reactions = pr.reactions_total or 0
    else:
        pr_comments = 0
        pr_reactions = 0
    # Weights: one issue-pain unit ≈ max community engagement on a single issue
    # (normalised to [0,1] by the issue_triage pipeline). PR comments and reactions
    # are supplementary signals — 100 comments ≈ 1 fully-engaged linked issue.
    score = round(issue_pain + 0.01 * pr_comments + 0.01 * pr_reactions, 4)
    return {
        "score": score,
        "issue_pain": round(issue_pain, 4),
        "linked_issues": len(fixed),
        "pr_comments": pr_comments,
        "pr_reactions": pr_reactions,
    }


def _cluster_pain(members: list[Pr]) -> dict:
    """Community Pain Score for a cluster, rolled up from per-PR scores.

    Sums each member's Pain Score so a single-PR cluster inherits its PR's score
    directly and multi-PR clusters reflect aggregate community pain.
    """
    total_score = 0.0
    total_issue_pain = 0.0
    total_linked_issues = 0
    total_pr_comments = 0
    total_pr_reactions = 0
    for pr in members:
        pain = pr_pain(pr)
        total_score += pain["score"]
        total_issue_pain += pain["issue_pain"]
        total_linked_issues += pain["linked_issues"]
        total_pr_comments += pain["pr_comments"]
        total_pr_reactions += pain["pr_reactions"]
    return {
        "score": round(total_score, 4),
        "issue_pain": round(total_issue_pain, 4),
        "linked_issues": total_linked_issues,
        "pr_comments": total_pr_comments,
        "pr_reactions": total_pr_reactions,
    }


def cluster_summaries() -> list[dict]:
    prs = data.prs()
    out = []
    for cid, c in data.clusters().items():
        members = [prs[n] for n in c.prs if n in prs]
        active = [r for r in members if r.state == "open"]
        state = gates.cluster_state(c, prs)
        dispositions: dict[str, int] = {}
        security = {"green": 0, "yellow": 0, "red": 0, "unknown": 0}
        security_prs = []  # per-merge-PR detail for the rollup hover popover
        for r in active:
            d = r.disposition
            if d:
                dispositions[d] = dispositions.get(d, 0) + 1
            if d == "merge":
                sec = r.section("security") or {}
                v = sec.get("verdict")
                security[(v or "unknown").lower()] += 1
                security_prs.append({
                    "pr": r.number,
                    "verdict": v or "UNKNOWN",
                    # None when no verdict exists (staleness doesn't apply);
                    # False = verdict exists but the merge gate treats it as
                    # not run (head moved, or older than SECURITY_MAX_AGE_DAYS)
                    "fresh": freshness.is_current(
                        r, "security",
                        max_age_days=gates.SECURITY_MAX_AGE_DAYS) if v else None,
                    "title": r.title,
                    "gating": True,  # merge-routed → its verdict gates the cluster
                    "findings": [{"severity": f.get("severity"), "title": f.get("title")}
                                 for f in sec.get("findings", [])],
                })
        pain = _cluster_pain(active)
        out.append({
            "cluster_id": cid,
            "root_problem": c.root_problem,
            "pr_count": len(active),
            "state": state,
            "outcome": c.outcome,
            "dispositions": dispositions,
            "security": security,
            "security_prs": security_prs,
            "analyzed_at": c.checked_at,
            "notes": c.notes,
            "pain_score": pain["score"],
            "pain_breakdown": {
                "issue_pain": pain["issue_pain"],
                "linked_issues": pain["linked_issues"],
                "pr_comments": pain["pr_comments"],
                "pr_reactions": pain["pr_reactions"],
            },
        })

    _STATE_RANK = {"ready": 0, "security-pending": 1, "needs-analysis": 2,
                   "awaiting-authors": 3, "needs-first-party-work": 4,
                   "blocked-on-decision": 5, "done": 6}
    out.sort(key=lambda c: (_STATE_RANK.get(c["state"], 9), -c["pr_count"]))
    return out


def cluster_detail(cid: int) -> dict | None:
    c = data.clusters().get(cid)
    if c is None:
        return None
    prs = data.prs()
    rows = [pr_row(n, prs[n]) for n in c.prs if n in prs]
    rows = [r for r in rows if r]
    buckets: dict[str, list] = {}
    for r in rows:
        # PRs already closed/merged upstream (state reconciled into the store,
        # #107) leave their active disposition bucket for a 'resolved' lane.
        if r.get("github_state") in ("closed", "merged"):
            buckets.setdefault("resolved", []).append(r)
        else:
            buckets.setdefault(r["disposition"] or "unanalyzed", []).append(r)
    return {
        "cluster_id": cid,
        "root_problem": c.root_problem,
        "outcome": c.outcome,
        "state": gates.cluster_state(c, prs),
        "rationale": c.rationale,
        "rationale_summary": c.rationale_summary,
        "notes": c.notes,
        "analyzed_at": c.checked_at,
        "prs": rows,
        "buckets": buckets,
    }


# ---------------------------------------------------------------------------
# PR detail + diff (read-only gh).
# ---------------------------------------------------------------------------
def pr_detail(n: int) -> dict | None:
    rec = data.prs().get(int(n))
    if rec is None:
        return None
    row = pr_row(n, rec)
    assert row is not None  # rec is non-None so pr_row always returns a dict
    row["base"] = rec.base
    row["security_detail"] = rec.section("security")
    row["safety_summary"] = _safety_summary(rec)
    row["verify_detail"] = verify_view.verify_detail(rec)
    row["verify_request"] = verify_view.verify_request_view(rec)
    row["fix_request"] = rec.fix_request
    row["analysis_detail"] = rec.section("analysis")
    row["summary"] = rec.section("summary")
    row["author_stats"] = data.author_stats(rec.author)
    sig = rec.signals or {}
    ds = sig.get("diffstat") or {}
    row["size"] = {"additions": ds.get("additions"), "deletions": ds.get("deletions"),
                   "changed_files": ds.get("changed_files")}

    # The rest each need a live round-trip — GitHub's CI check runs, its file
    # list (CODEOWNERS), and the PR body. They're independent, so fan them out:
    # wall time is one round-trip, not the sum of three. The CODEOWNERS and body
    # calls only fire when their cached/ingested value is missing, so most warm
    # PRs spend their latency on the CI fetch alone. Reviewer feedback comes from
    # the store's reviews section, which ingest and the live sweep keep current.
    hm_known, hm_cached = _human_merge_cached(rec)
    # Which changed paths pinned the tier — the detail view's "why this tier".
    row["risk_tier_paths"] = (_facets(rec).get("risk") or {}).get("pinned_by") or []
    # The bulk load omits meta.body; read it from the store for this one PR. A
    # live GitHub fetch is the last resort, for a PR whose body was never ingested.
    body = rec.body or data.pr_body(n)
    failed_live: list[str] = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        detail_fut = pool.submit(_ci_checks, rec.head_sha)
        # authoritative CODEOWNERS + tier source; live file list only when uncached (#15/#26)
        paths_fut = None if hm_known else pool.submit(live_changed_paths, n)
        body_fut = pool.submit(_pr_body_live, n) if body is None else None  # ingested lazily

        # Resolve each future on its own — a timeout/failure on any one degrades to
        # the cached/ingested value instead of taking down the whole endpoint.
        row["ci_checks"] = _resolve_live(detail_fut, [], failed_live, "ci")
        row["reviews_detail"] = {rid: {"entry": rec.review_entry(rid), "digest": d}
                                 for rid, d in (row.get("reviews") or {}).items()}
        if hm_known:
            row["human_merge"] = hm_cached
        else:
            live_paths = _resolve_live(paths_fut, None, failed_live, "human_merge")
            if live_paths is not None:
                row["human_merge"] = codeowners.human_merge(live_paths)
                live_risk = risktier.tier_facet(live_paths)
                row["risk_tier"] = live_risk["tier"]
                row["risk_tier_paths"] = live_risk["pinned_by"]
        row["body"] = body if body_fut is None else _resolve_live(body_fut, body, failed_live, "body")
    # Which live bits couldn't refresh (empty when all succeeded). The app shows
    # a "couldn't refresh live data" note rather than the operator seeing a 500.
    row["live_refresh_failed"] = failed_live

    # Human-initiated merge verdict for the action bar (gates.merge_eligibility is
    # the single source of truth). The executor re-checks CODEOWNERS against a live
    # file list on merge; here we layer the already-resolved human_merge on top so
    # a gated path shows as blocked without a second round-trip.
    gate = _merge_gate_fields(rec)
    hm = row.get("human_merge")
    if hm and hm.get("required"):
        gate["ok"], gate["overridable"], gate["override_kind"] = False, False, None
        gate["reason"] = f"requires manual merge by a code owner ({' '.join(hm['owners'])})"
    row["merge_gate"] = gate
    return row


def _safety_summary(rec: Pr) -> dict:
    """Operator-facing security banner. A verdict outside the merge-recency window
    (gates.SECURITY_MAX_AGE_DAYS) still renders, but says why it no longer counts
    for merge — so the banner never contradicts the merge gate below it."""
    sec = rec.section("security")
    if not sec:
        return {"verdict": None, "level": "unknown",
                "headline": "Not yet security-reviewed",
                "detail": "Deep security review runs on clean merge candidates (GATE → SECURITY)."}
    why_stale = freshness.currency_failure(rec, "security",
                                           max_age_days=gates.SECURITY_MAX_AGE_DAYS)
    v, n = sec.get("verdict"), len(sec.get("findings", []))
    if v == "GREEN":
        if why_stale:
            return {"verdict": v, "level": "safe",
                    "headline": "Likely safe at last review — no concerns flagged",
                    "detail": ("The multi-agent security review found nothing concerning, "
                               f"but it is {why_stale} — it no longer counts for merge. "
                               "Re-run SECURITY for a current verdict.")}
        return {"verdict": v, "level": "safe", "headline": "Likely safe — no concerns flagged",
                "detail": "The multi-agent security review found nothing concerning."}
    note = f" (The review is {why_stale} — re-run SECURITY for a current verdict.)" if why_stale else ""
    if v == "YELLOW":
        return {"verdict": v, "level": "caution",
                "headline": f"Proceed with care — {n} concern{'s' if n != 1 else ''} flagged",
                "detail": "Non-blocking concerns. Read them before merging." + note}
    if v == "RED":
        return {"verdict": v, "level": "risk",
                "headline": f"Risky — {n} serious concern{'s' if n != 1 else ''}",
                "detail": "Serious issues flagged. Do not merge without addressing them." + note}
    return {"verdict": v, "level": "unknown", "headline": str(v), "detail": note.strip()}


MAX_FILES_API = 100  # cap reconstructed diffs so huge PRs stay responsive


def _diff_from_files_api(n: int) -> dict:
    """Fallback for PRs whose diff `gh pr diff` refuses (>300 files / HTTP 406)."""
    res = run(["gh", "api", f"repos/{settings.repo()}/pulls/{n}/files?per_page=100", "--paginate"], timeout=120)
    if res.returncode != 0 or not res.stdout.strip():
        return {"diff": "", "error": "could not fetch file list", "file_count": 0,
                "truncated": False, "source": "files-api"}
    raw = res.stdout.replace("][", ",")  # gh --paginate concatenates arrays
    try:
        files = json.loads(raw)
    except json.JSONDecodeError:
        return {"diff": "", "error": "could not parse file list", "file_count": 0,
                "truncated": False, "source": "files-api"}
    total = len(files)
    parts = []
    for f in files[:MAX_FILES_API]:
        path, old = f.get("filename", "?"), f.get("previous_filename", f.get("filename", "?"))
        status = f.get("status")
        a = "/dev/null" if status == "added" else f"a/{old}"
        b = "/dev/null" if status == "removed" else f"b/{path}"
        header = f"diff --git a/{old} b/{path}\n--- {a}\n+++ {b}\n"
        patch = f.get("patch")
        if patch:
            parts.append(header + patch + ("\n" if not patch.endswith("\n") else ""))
        else:
            parts.append(header)
    return {"diff": "".join(parts), "file_count": total,
            "truncated": total > MAX_FILES_API, "source": "files-api"}


def get_diff(n: int) -> dict:
    rec = data.prs().get(int(n))
    sha = (rec.head_sha if rec is not None else None) or f"pr{n}"
    DIFF_CACHE.mkdir(parents=True, exist_ok=True)
    cached = DIFF_CACHE / f"{sha}.diff"
    if cached.exists():
        return {"diff": cached.read_text(), "source": "cache"}
    res = run(["gh", "pr", "diff", str(n), "--repo", settings.repo()], timeout=120)
    if res.returncode == 0 and res.stdout.strip():
        cached.write_text(res.stdout)
        return {"diff": res.stdout, "source": "gh"}
    return _diff_from_files_api(n)
