"""INGEST: fetch open issues (read-only), reconcile closures, and compute
deterministic facts into the store. The store-writing halves (ingest_records,
reconcile_closures) are pure and unit-tested; main() adds the live fetch + the
candidate-PR join. Mirrors pipeline/ingest.py.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import TYPE_CHECKING

from issue_triage import config
from issue_triage import fetch_issues
from issue_triage import issue_model
from issue_triage import link_prs
from issue_triage import repro_grade
from issue_triage import summarize_issues
from issue_triage.issue_store import IssueStore
from pipeline.storekit import now as _now

if TYPE_CHECKING:
    from pipeline.store import Store


def _meta(raw: dict) -> dict:
    return {
        "title": raw.get("title", ""),
        "body": raw.get("body") or "",
        "state": raw.get("state", "open"),
        "state_reason": raw.get("state_reason"),
        "author": raw.get("author", ""),
        "labels": raw.get("labels") or [],
        "comments": raw.get("comments", 0),
        "reactions_total": raw.get("reactions_total", 0),
        "thumbs_up": raw.get("thumbs_up", 0),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "url": f"https://github.com/{config.repo()}/issues/{raw['number']}",
    }


def _facts_unchanged(iss: issue_model.Issue, meta: dict, summary: dict,
                     repro: dict) -> bool:
    """True when re-ingesting reproduces identical meta/summary/repro, so the
    write is a no-op. `links` is intentionally NOT compared: the existing snapshot
    omits the (large) candidate arrays to keep the corpus load off Supabase's
    statement timeout, so an unchanged issue keeps its stored links until its
    meta/summary/repro move. Ignores the checked_at / against_updated_at stamps."""
    rec = iss.rec

    def same(section: str, new: dict) -> bool:
        stored = rec.get(section)
        return stored is not None and all(stored.get(k) == v for k, v in new.items())

    return same("meta", meta) and same("summary", summary) and same("repro", repro)


def ingest_records(store: IssueStore, raws: list[dict], prs: list[dict]) -> int:
    """Upsert each normalized issue raw whose facts changed: meta + summary +
    repro + links. meta/summary/repro are computed for every raw (cheap) and
    compared to the store; only issues that differ are written, and links are
    recomputed only for those — so an unchanged re-ingest skips the per-issue
    upsert, which is the loop's dominant cost against a networked store. The
    corpus is loaded once WITHOUT its candidate arrays (loading them all
    intermittently exceeds the store's statement timeout); each changed issue
    lands as a single write (Issue.apply_facts) on one reused connection
    (store.batch). A moved updated_at (or edited body) re-stamps the facts so
    freshness flips. Returns how many issues were written."""
    if not raws:
        return 0
    existing = store.all_issues(omit_candidates=True)
    refs = link_prs.parse_refs(prs)  # each PR body parsed once, not once per issue
    written = 0
    with store.batch():
        for raw in raws:
            n = raw["number"]
            meta = _meta(raw)
            s = summarize_issues.summarize({"number": n, "title": meta["title"], "body": meta["body"]})
            summary = {"subsystem": s["subsystem"], "identifiers": s["identifiers"]}
            repro = repro_grade.grade_repro(meta["body"])
            prev = existing.get(n)
            if prev is not None and _facts_unchanged(prev, meta, summary, repro):
                continue
            links = link_prs.candidate_prs(
                n, s["subsystem"], prs, refs, issue_text=f"{meta['title']}\n{meta['body']}")
            iss = prev or issue_model.Issue(store, {"issue": int(n)})
            iss.apply_facts(meta, summary=summary, repro=repro, links=links)
            written += 1
    return written


def reconcile_closures(store: IssueStore, open_now: set[int], prs: list[dict],
                       fetch_one: Callable[[int], dict | None] = fetch_issues.fetch_issue,
                       ) -> int:
    """Issues in the store still marked open but absent from the open fetch have
    closed upstream (or fell past fetch_all's pagination cap); refetch each one
    and upsert it through ingest_records, so its meta AND derived facts (summary,
    repro, links) match the refetched content. An unfetchable issue is left
    untouched. Returns how many issues changed state."""
    refetched: list[dict] = []
    transitions = 0
    for n, iss in store.all_issues(omit_candidates=True).items():
        if n in open_now or iss.state != "open":
            continue
        raw = fetch_one(n)
        if raw is None:
            continue
        if raw.get("state", "open") != iss.state:
            transitions += 1
        refetched.append(raw)
    ingest_records(store, refetched, prs)
    return transitions


def _load_prs(pr_store: Store | None = None) -> list[dict]:
    """The PR corpus the issue<->PR linker matches against: every open or merged
    PR in the SQL PR store (a merged fixer stays linked — the durable evidence an
    issue is likely fixed), each carrying its `state` and tagged with the shared
    subsystem taxonomy (so subsystem-match candidates work). Bodies are rehydrated
    in one batch — all_prs omits meta.body — because the explicit `Fixes #N`
    parse reads them."""
    from pipeline.store import Store
    store = pr_store or Store()
    keep = {n: pr for n, pr in store.all_prs().items() if pr.state in ("open", "merged")}
    bodies = store.pr_bodies(list(keep))
    out: list[dict] = []
    for n, pr in keep.items():
        title, body = pr.title or "", bodies.get(n) or ""
        out.append({"number": n, "title": title, "body": body, "state": pr.state,
                    "subsystem": summarize_issues.classify_subsystem(title, body)})
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", default=None, help="store root override (tests/smoke)")
    ap.add_argument("--max", type=int, default=None,
                    help="cap issues fetched (smoke runs); skips the closure "
                         "reconciliation sweep — a capped fetch would make every "
                         "uncapped issue look vanished")
    args = ap.parse_args(argv)
    store = IssueStore(args.store) if args.store else IssueStore()
    started = _now()
    print("fetching open issues…", flush=True)
    raws = fetch_issues.fetch_all(max_issues=args.max)
    prs = _load_prs()
    print(f"open issues: {len(raws)} | PR corpus: {len(prs)} | upserting…", flush=True)
    n = ingest_records(store, raws, prs)
    transitions = 0
    if args.max is None:
        print("reconciling closures…", flush=True)
        transitions = reconcile_closures(store, {r["number"] for r in raws}, prs)
    phase = "ingest" if args.max is None else "ingest:smoke"
    stats = {"open_fetched": len(raws), "upserted": n, "state_transitions": transitions}
    store.append_run({"phase": phase, "started": started, "finished": _now(),
                      "stats": stats})
    dest = store.engine.url.host or store.root  # networked store: host; SQLite: local root
    print(f"ingested {len(raws)} open issues ({n} changed, written), "
          f"{transitions} state transitions -> {dest}")


if __name__ == "__main__":
    main()
