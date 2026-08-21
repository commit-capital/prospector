"""Phase 0 — INGEST (deterministic, cheap, run anytime).

Fetches open PRs (including drafts) from upstream (read-only gh) and issue links
from issue_triage; upserts them into the store. Facts are stamped against
head_sha by the store, so a moved head automatically stales analysis/security
without any explicit invalidation here.

Closed/merged PRs already in the store get their meta.state updated (the
record is history, not deleted). Brand-new closed PRs are not ingested —
they're dupe/already-fixed *evidence*, consulted live during ANALYZE.

A full run reconciles the whole open corpus (and the closed-state sweep), then
chains the Greptile reviewed-SHA backfill and the issue ingest. `--new` fetches
that corpus and upserts only PRs absent from the store, while `--prs` fetches
each named PR directly and reconciles its live CI and Greptile signals. Both
skip the closed-state sweep, the backfill, and the issue-ingest chain.
Targeted refreshes write each PR as it is completed; corpus refreshes stage
the records and persist bounded chunks.

Usage:
  uv run python pipeline/ingest.py [--max N] [--store DIR]
  uv run python pipeline/ingest.py --new          # only new arrivals
  uv run python pipeline/ingest.py --prs 51,52,53 # only these PRs
  uv run python pipeline/ingest.py --backfill-greptile-data
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import TYPE_CHECKING, TypedDict

from pipeline import settings
from pipeline import diffpaths
from pipeline import greptile
from pipeline import live_prs
from pipeline import model
from pipeline import review_policy
from pipeline.gh import fetch_pr, gh_json, operator_env
from pipeline.store import Store
from pipeline.storekit import now as _now

if TYPE_CHECKING:
    from issue_triage.issue_store import IssueStore
    from pipeline.model import Pr


_UPSERT_BATCH_SIZE = 250
_LIVE_MERGEABLE = {"MERGEABLE": True, "CONFLICTING": False}


class _UpsertStats(TypedDict):
    upserted: int
    drafts: int
    open_ids: set[int]


# ---------------------------------------------------------------------------
# Pure transforms (tested)
# ---------------------------------------------------------------------------
def meta_from_gh(pr: dict) -> dict:
    state = "merged" if pr.get("merged_at") else pr.get("state", "open")
    reactions = pr.get("reactions") or {}
    return {
        "title": pr.get("title") or "(untitled)",
        "body": (pr.get("body") or "")[:20000] or None,
        "author": (pr.get("user") or {}).get("login"),
        "state": state,
        "draft": bool(pr.get("draft")),
        "head_sha": (pr.get("head") or {}).get("sha"),
        "base": (pr.get("base") or {}).get("ref"),
        "url": pr.get("html_url"),
        "created_at": pr.get("created_at"),
        "updated_at": pr.get("updated_at"),
        "comments": pr.get("comments"),
        "reactions_total": reactions.get("total_count"),
    }


_CI_FAIL_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"})
_CI_OK_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


def _ci_from_github_data(check_runs: list[dict], statuses: list[dict]) -> str | None:
    """Combine GitHub's check-runs API and legacy commit-status API into a single
    CI verdict ('passing' | 'failing' | 'pending'), the way the PR Checks tab
    does. Returns None when GitHub reports no checks at all — the caller then
    keeps the stored value rather than inventing a verdict. Failure outranks
    pending outranks passing; skipped/neutral don't fail a run."""
    saw_any = saw_fail = saw_pending = False
    for run in check_runs:
        saw_any = True
        if run.get("status") != "completed":
            saw_pending = True
        elif run.get("conclusion") in _CI_FAIL_CONCLUSIONS:
            saw_fail = True
        elif run.get("conclusion") not in _CI_OK_CONCLUSIONS:
            saw_pending = True  # completed with an unrecognized conclusion — be cautious
    for st in statuses:
        saw_any = True
        if st.get("state") in ("failure", "error"):
            saw_fail = True
        elif st.get("state") == "pending":
            saw_pending = True
    if not saw_any:
        return None
    if saw_fail:
        return "failing"
    if saw_pending:
        return "pending"
    return "passing"


def drift_from_signals(signals: dict) -> dict:
    """Mechanical drift only. 'already-fixed' is a semantic judgment made by
    ANALYZE (with evidence); ingest can only see mergeability."""
    return {"state": "applicable" if signals.get("mergeable", True) else "conflicts"}


def issues_by_pr(links: list[dict]) -> dict[int, list[dict]]:
    """Invert a list of per-issue link records ({issue, pain, candidates}) into the
    PR → linked-issues map. Each candidate carries how the PR was matched
    ('explicit' | 'body-ref' | 'issue-ref' | 'subsystem'); downstream credits
    pain only for 'explicit'."""
    out: dict[int, list[dict]] = {}
    for link in links:
        for cand in link.get("candidates", []) or []:
            out.setdefault(int(cand["pr"]), []).append({
                "issue": link["issue"],
                "pain": link.get("pain"),
                "how": cand.get("how"),
            })
    return out


def _build_signals(*, existing_signals: dict | None, ci_override: str | None,
                   greptile_override: int | None,
                   greptile_reviewed_sha: str | None,
                   mergeable_override: bool | None = None,
                   diffstat_override: dict | None = None,
                   has_tests_override: bool | None = None,
                   head_moved: bool = False) -> dict | None:
    """The PR's signals section for an upsert, or None when there's nothing to
    write. Built when a refresh supplied a live override: a copy of the PR's
    existing signals with those current-head facts overlaid.
    Diff-derived fields are never carried into a newly stamped section: they are
    replaced with values computed at the fetched head, or removed when that data
    was unavailable."""
    has_override = (ci_override is not None or mergeable_override is not None
                    or greptile_override is not None
                    or greptile_reviewed_sha is not None
                    or diffstat_override is not None
                    or has_tests_override is not None)
    if not has_override:
        return None
    sig = dict(existing_signals or {})
    if head_moved:
        sig.pop("ci", None)
        sig.pop("mergeable", None)
    # These values describe one exact diff. Keeping them while `_stage_pr`
    # stamps the whole section at a moved head would make stale data look fresh.
    sig.pop("diffstat", None)
    sig.pop("has_tests", None)
    if ci_override is not None:
        sig["ci"] = ci_override
    if mergeable_override is not None:
        sig["mergeable"] = mergeable_override
    if greptile_override is not None:
        sig["greptile"] = greptile_override
    if greptile_reviewed_sha is not None:
        sig["greptile_reviewed_sha"] = greptile_reviewed_sha
    if diffstat_override is not None:
        sig["diffstat"] = diffstat_override
    if has_tests_override is not None:
        sig["has_tests"] = has_tests_override
    return sig


def _diffstat_from_gh(gh_pr: dict) -> dict | None:
    """The REST PR's exact aggregate diffstat, or None when incomplete."""
    values = (gh_pr.get("additions"), gh_pr.get("deletions"), gh_pr.get("changed_files"))
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return None
    return {"additions": values[0], "deletions": values[1], "changed_files": values[2]}


def _stage_pr(store: Store, gh_pr: dict, existing: Pr | None,
              issues: list[dict] | None = None, *,
              ci_override: str | None = None,
              mergeable_override: bool | None = None,
              greptile_override: int | None = None,
              greptile_reviewed_sha: str | None = None,
              diffstat_override: dict | None = None,
              has_tests_override: bool | None = None) -> Pr:
    """Merge GitHub facts into a new or existing PR without writing it."""
    n = int(gh_pr["number"])
    meta = meta_from_gh(gh_pr)
    head_moved = existing is not None and existing.head_sha != meta.get("head_sha")
    sig = _build_signals(
        existing_signals=existing.signals if existing is not None else None,
        ci_override=ci_override, mergeable_override=mergeable_override,
        greptile_override=greptile_override, greptile_reviewed_sha=greptile_reviewed_sha,
        diffstat_override=diffstat_override, has_tests_override=has_tests_override,
        head_moved=head_moved)
    drift = drift_from_signals(sig) if sig is not None else None
    pr = existing if existing is not None else model.Pr(store, {"pr": n})
    pr.stage_facts(meta, signals=sig, drift=drift, issues=issues)
    return pr


def upsert_pr(store: Store, gh_pr: dict,
              issues: list[dict] | None = None,
              *, ci_override: str | None = None,
              mergeable_override: bool | None = None,
              greptile_override: int | None = None,
              greptile_reviewed_sha: str | None = None,
              diffstat_override: dict | None = None,
              has_tests_override: bool | None = None) -> Pr:
    """Upsert one PR's meta (+signals/drift/issues). Returns the Pr. `ci_override`
    is GitHub's authoritative CI verdict when the caller has fetched one (the
    single-PR refresh path). `greptile_override` is Greptile's own on-PR
    confidence score. `greptile_reviewed_sha` is the commit Greptile reviewed,
    used to detect when that score predates recent commits. The diff overrides
    are derived together from the fetched head and prevent old-head values from
    inheriting its section stamp."""
    existing = store.load_pr(int(gh_pr["number"]))
    pr = _stage_pr(
        store, gh_pr, existing, issues,
        ci_override=ci_override, mergeable_override=mergeable_override,
        greptile_override=greptile_override,
        greptile_reviewed_sha=greptile_reviewed_sha,
        diffstat_override=diffstat_override, has_tests_override=has_tests_override)
    store.save_pr(pr)
    return pr


def refresh_prs(store: Store, numbers: list[int]) -> list[dict]:
    """Refresh specific PRs from live gh + issue links (used by the
    single-cluster re-run). Returns one {pr, moved, old_sha, new_sha} per PR.
    CI, Greptile, diffstat, and test presence are reconciled before the signals
    section is stamped. A moved head auto-stales summary/analysis/security via
    the store's against_head_sha stamping — no explicit invalidation here."""
    out: list[dict] = []
    for n in numbers:
        n = int(n)
        before = store.load_pr(n)
        old_sha = before.head_sha if before else None
        gh_pr = fetch_pr(n)
        if gh_pr is None:
            raise RuntimeError(f"gh api pulls/{n} failed or returned no PR")
        issue_links = load_issue_links(prs=[gh_pr])
        # GitHub is the source of truth for CI: reconcile against its live
        # verdict for this exact head so a stale 'failing' can't false-block.
        head_sha = (gh_pr.get("head") or {}).get("sha")
        ci = gh_ci_status(head_sha) if head_sha else None
        raw_mergeable = gh_pr.get("mergeable")
        mergeable = raw_mergeable if isinstance(raw_mergeable, bool) else None
        diffstat = _diffstat_from_gh(gh_pr)
        has_tests = None
        if head_sha:
            # Targeted refreshes already need this exact diff for their
            # summarize/analyze tail. Derive the signal before the atomic
            # signals write so the section's new head stamp is truthful.
            from pipeline import diff_cache
            paths = diff_cache.fetch_diff_paths(n, head_sha, store=store)
            if paths is not None:
                has_tests = diffpaths.has_tests(paths)
        # Read Greptile's own confidence score + reviewed commit directly from
        # the PR, so staleness is known against the reviewed commit. Only when
        # Greptile is the configured review provider; otherwise there is no such
        # score to read.
        if review_policy.active().provider == "greptile":
            greptile_score, greptile_sha = greptile.fetch_greptile_verdict(n)
        else:
            greptile_score, greptile_sha = None, None
        rec = upsert_pr(store, gh_pr, issue_links.get(n, []),
                        ci_override=ci, mergeable_override=mergeable,
                        greptile_override=greptile_score,
                        greptile_reviewed_sha=greptile_sha,
                        diffstat_override=diffstat, has_tests_override=has_tests)
        new_sha = rec.head_sha
        out.append({"pr": n, "moved": old_sha != new_sha,
                    "old_sha": old_sha, "new_sha": new_sha})
    return out


def gh_ci_status(sha: str) -> str | None:
    """GitHub's authoritative CI verdict for `sha` ('passing' | 'failing' |
    'pending'), or None when GitHub has no checks for it or can't be reached — the
    caller then keeps the stored value. Best-effort: a gh hiccup never breaks a
    refresh. Combines the check-runs API (filter=latest drops superseded re-runs)
    and the legacy commit-status API."""
    runs = gh_json(f"repos/{settings.repo()}/commits/{sha}/check-runs?per_page=100&filter=latest")
    status = gh_json(f"repos/{settings.repo()}/commits/{sha}/status")
    if runs is None and status is None:
        return None  # couldn't reach GitHub at all — keep the stored verdict
    check_runs = (runs or {}).get("check_runs", [])
    statuses = (status or {}).get("statuses", [])
    return _ci_from_github_data(check_runs, statuses)


# ---------------------------------------------------------------------------
# Network helpers (thin, untested)
# ---------------------------------------------------------------------------
def fetch_open_prs(max_n: int | None = None) -> list[dict]:
    """All open PRs via gh (read-only, the operator's local gh login). Paginated REST."""
    out: list[dict] = []
    page = 1
    while True:
        res = subprocess.run(
            ["gh", "api", f"repos/{settings.repo()}/pulls?state=open&per_page=100&page={page}"],
            capture_output=True, text=True, timeout=120, env=operator_env())
        if res.returncode != 0:
            raise RuntimeError(f"gh api failed: {res.stderr.strip()[:300]}")
        batch = json.loads(res.stdout)
        out.extend(batch)
        print(f"  page {page}: {len(batch)} PRs (total {len(out)})", flush=True)
        if len(batch) < 100 or (max_n and len(out) >= max_n):
            break
        page += 1
    return out[:max_n] if max_n else out


def load_issue_links(iss_store: IssueStore | None = None,
                     prs: list[dict] | None = None) -> dict[int, list[dict]]:
    """PR → linked issues, read from the SQL issue store. Each issue carries its
    candidate PRs (stamped by issue_ingest via set_links) and belongs to a cluster
    whose pain applies to it; invert that issue→PRs mapping into the PR→issues map
    the ingest attaches to each PR. When live PRs are supplied, their current
    bodies authoritatively refresh direct explicit/body-ref evidence."""
    from issue_triage.issue_store import IssueStore
    st = iss_store or IssueStore()
    pain_by_cluster = {cid: cl.pain for cid, cl in st.all_issue_clusters().items()}
    issues = st.all_issues()
    issue_pain = {
        n: pain_by_cluster.get(iss.cluster_id) if iss.cluster_id is not None else None
        for n, iss in issues.items()
    }
    out = issues_by_pr([
        {"issue": iss.number, "pain": issue_pain[iss.number],
         "candidates": iss.candidate_prs}
        for iss in issues.values()
        if iss.candidate_prs
    ])

    # The issue store's candidate snapshot is refreshed when the issue changes,
    # but a newly opened or newly edited PR must not wait for that unrelated
    # event. Parse the live PR bodies too, validate #N against the issue corpus,
    # and overlay stronger direct evidence over a weak subsystem match.
    from issue_triage import link_prs
    for pr in prs or []:
        n = int(pr["number"])
        by_issue = {entry["issue"]: entry for entry in out.get(n, [])
                    if entry.get("how") not in ("explicit", "body-ref")}
        refs = link_prs.classify_issue_refs(pr.get("body"))
        for issue_n in sorted(refs.keys() & issues.keys()):
            how = refs[issue_n]
            by_issue[issue_n] = {"issue": issue_n, "pain": issue_pain[issue_n], "how": how}
        if by_issue:
            out[n] = list(by_issue.values())
        else:
            out.pop(n, None)
    return out


def _parse_pr_ids(spec: str) -> set[int]:
    """Parse a comma-separated `--prs` spec into a set of PR numbers."""
    return {int(p) for p in spec.split(",") if p.strip()}


def _select_new_prs(gh_prs: list[dict], existing_ids: set[int]) -> list[dict]:
    """Open PRs that are absent from the store."""
    return [pr for pr in gh_prs if int(pr["number"]) not in existing_ids]


def _upsert_all(store: Store, prs: list[dict], issue_links: dict[int, list[dict]],
                existing_prs: dict[int, Pr], *,
                live_facts: dict[int, dict] | None = None,
                batch_size: int = _UPSERT_BATCH_SIZE) -> _UpsertStats:
    """Stage and bulk-upsert `prs`, returning counts plus their open IDs.

    `existing_prs` is the caller's corpus snapshot. Each committed chunk stays
    durable if a long ingest is interrupted.
    """
    staged: list[Pr] = []
    drafts = 0
    open_ids: set[int] = set()
    facts = live_facts or {}
    for gh_pr in prs:
        n = int(gh_pr["number"])
        live = facts.get(n) or {}
        head_sha = (gh_pr.get("head") or {}).get("sha")
        current = live if live.get("head") == head_sha else {}
        live_mergeable = _LIVE_MERGEABLE.get(current.get("mergeable") or "")
        rec = _stage_pr(
            store, gh_pr, existing_prs.get(n), issue_links.get(n, []),
            ci_override=current.get("ci"), mergeable_override=live_mergeable,
            diffstat_override=current.get("diffstat"),
            has_tests_override=current.get("has_tests"))
        staged.append(rec)
        open_ids.add(n)
        if rec.draft:
            drafts += 1

    for start in range(0, len(staged), batch_size):
        store.save_prs_many(staged[start:start + batch_size])

    return {"upserted": len(staged), "drafts": drafts, "open_ids": open_ids}


def _targeted_ingest(store: Store, args: argparse.Namespace, started: str) -> int:
    """Upsert only new (`--new`) or named (`--prs`) PRs, without reconciling
    closed-PR state across the full store."""
    if args.prs:
        requested = sorted(_parse_pr_ids(args.prs))
        if args.new:
            existing_ids = set(store.all_prs())
            requested = [n for n in requested if n not in existing_ids]
        print(f"refreshing {len(requested)} requested PRs…", flush=True)
        refreshed = refresh_prs(store, requested)
        drafts = sum(1 for n in requested
                     if (pr := store.load_pr(n)) is not None and pr.draft)
        stats = {"targeted": len(requested), "upserted": len(refreshed), "drafts": drafts}
        mode = "new" if args.new else "prs"
        store.append_run({"phase": f"ingest:{mode}", "started": started,
                          "finished": _now(), "stats": stats})
        print(f"done: {stats}")
        return 0

    print("fetching open PRs…", flush=True)
    gh_prs = fetch_open_prs(args.max)
    existing_prs = store.all_prs()
    targets = _select_new_prs(gh_prs, set(existing_prs))
    issue_links = load_issue_links(prs=targets)
    print(f"open PRs: {len(gh_prs)} | targeting: {len(targets)}")
    live, _ = live_prs.fetch([int(pr["number"]) for pr in targets])
    with store.batch():
        counts = _upsert_all(store, targets, issue_links, existing_prs, live_facts=live)
    upserted, drafts = counts["upserted"], counts["drafts"]

    stats = {"open_fetched": len(gh_prs), "targeted": len(targets),
             "upserted": upserted, "drafts": drafts,
             "live_facts_fetched": len(live), "live_facts_missing": len(targets) - len(live)}
    store.append_run({"phase": "ingest:new", "started": started,
                      "finished": _now(), "stats": stats})
    print(f"done: {stats}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", default=None, help="store root override (tests)")
    ap.add_argument("--max", type=int, default=None, help="cap PRs fetched (smoke runs)")
    ap.add_argument("--new", action="store_true",
                    help="upsert only open PRs not already in the store (new arrivals); "
                         "reuses the cheap bulk fetch, skips the closed-state sweep")
    ap.add_argument("--prs", default=None,
                    help="refresh only these comma-separated PR numbers directly, including "
                         "live CI and Greptile signals; skips the closed-state sweep")
    ap.add_argument("--backfill-greptile-data", action="store_true",
                    help="fetch the Greptile-reviewed commit SHA for open scored PRs "
                         "missing it or holding one that is not the current head, so "
                         "staleness becomes known; skips the full ingest")
    ap.add_argument("--skip-issues", action="store_true",
                    help="skip the issue ingest (issue_triage/issue_ingest.py) that a "
                         "full ingest chains after the PR sweep, so one refresh covers "
                         "both corpora")
    args = ap.parse_args(argv)

    store = Store(args.store) if args.store else Store()
    started = _now()

    if args.backfill_greptile_data:
        if review_policy.active().provider != "greptile":
            print("Greptile is not the configured review provider — nothing to backfill.")
            return 0
        print("backfilling Greptile reviewed-SHA for scored PRs missing it or off-head…",
              flush=True)
        stats = greptile.backfill_greptile_data(store)
        store.append_run({"phase": "backfill-greptile-data", "started": started,
                          "finished": _now(), "stats": stats})
        print(f"done: {stats}")
        return 0

    if args.new or args.prs:
        return _targeted_ingest(store, args, started)

    print("fetching open PRs…", flush=True)
    gh_prs = fetch_open_prs(args.max)
    live, _ = live_prs.fetch([int(pr["number"]) for pr in gh_prs])
    issue_links = load_issue_links(prs=gh_prs)
    print(f"open PRs: {len(gh_prs)} | PRs with issue links: {len(issue_links)}")

    transitions = 0
    with store.batch():
        existing_prs = store.all_prs()
        counts = _upsert_all(store, gh_prs, issue_links, existing_prs, live_facts=live)
        upserted, drafts, open_now = counts["upserted"], counts["drafts"], counts["open_ids"]

        # PRs in the store that are no longer in the open list → closed or merged;
        # refresh their meta so state transitions are recorded. The pre-upsert
        # snapshot is also the closure-candidate corpus.
        for n, rec in existing_prs.items():
            if n in open_now or rec.state != "open":
                continue
            gh_pr = fetch_pr(n)
            if gh_pr is not None:
                rec.set_meta(meta_from_gh(gh_pr))
                transitions += 1

    stats = {"open_fetched": len(gh_prs), "upserted": upserted,
             "drafts": drafts, "state_transitions": transitions,
             "live_facts_fetched": len(live), "live_facts_missing": len(gh_prs) - len(live)}
    store.append_run({"phase": "ingest", "started": started, "finished": _now(), "stats": stats})
    print(f"done: {stats}")

    # A full ingest keeps the reviewed-SHA fact fresh too: chain the backfill
    # (its own run ledger entry) so a bulk-ingested PR doesn't read "not
    # current" indefinitely. It self-scopes to open scored PRs whose SHA is
    # missing or anchored off the current head, so a corpus with nothing to
    # backfill costs one cheap store scan (#21).
    if review_policy.active().provider == "greptile":
        print("backfilling Greptile reviewed-SHA for scored PRs missing it or off-head…",
              flush=True)
        backfill_started = _now()
        backfill_stats = greptile.backfill_greptile_data(store, existing_prs)
        store.append_run({"phase": "backfill-greptile-data", "started": backfill_started,
                          "finished": _now(), "stats": backfill_stats})
        print(f"done: {backfill_stats}")

    # A full ingest refreshes both corpora: chain the issue ingest (its own run
    # ledger entry, its own store rows) so issue state can't silently drift while
    # PRs stay fresh (#412). Targeted modes (--new/--prs/--backfill) return above.
    if not args.skip_issues:
        print("running issue ingest…", flush=True)
        from issue_triage import issue_ingest
        issue_ingest.main([])
    return 0


if __name__ == "__main__":
    sys.exit(main())
