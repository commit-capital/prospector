"""INGEST: fetch open issues (read-only), reconcile closures, and compute
deterministic facts into the store. The store-writing halves (ingest_records,
reconcile_closures) are pure and unit-tested; main() adds the live fetch + the
candidate-PR join. Mirrors pipeline/ingest.py.
"""
from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, NamedTuple

from issue_triage import config
from issue_triage import fetch_issues
from issue_triage import issue_model
from issue_triage import link_prs
from issue_triage import pr_index
from issue_triage import repro_grade
from issue_triage import summarize_issues
from issue_triage.issue_store import IssueStore
from pipeline import progress
from pipeline.storekit import now as _now

if TYPE_CHECKING:
    from pipeline.store import Store

# How many times one issue's compare-and-swap is re-read and re-staged before the
# ingest gives up on it. A shared store carries several writers, so losing a swap
# is routine; losing three in a row on one issue is a contended issue, not a bug.
SWAP_ATTEMPTS = 3

# What a swap did: wrote the facts, found no such issue, or lost every attempt.
SwapResult = Literal["written", "absent", "lost"]


class IngestCounts(NamedTuple):
    written: int
    swap_lost: int


def _partial(raw: dict) -> bool:
    """True for a raw fetched by a transport that cannot see every fact — the REST
    single-issue refetch, which reports neither the closing references, the
    body's edit time, nor its comments' authors."""
    return raw.get("github_links") is None


def _meta(raw: dict, prev: issue_model.Issue | None) -> dict:
    """The meta section to write for `raw`. A partial raw carries no edit time and
    no content time, so an issue already in the store keeps the ones it has,
    except that a title or body differing from the stored one, or a reopen, moves
    the content time to the raw's updated_at."""
    title = raw.get("title", "")
    body = raw.get("body") or ""
    state = raw.get("state", "open")
    last_edited_at = raw.get("last_edited_at")
    content_updated_at = raw.get("content_updated_at")
    if prev is not None and _partial(raw):
        last_edited_at = prev.last_edited_at
        content_updated_at = prev.content_updated_at
        if ((title, body) != (prev.title, prev.body or "")
                or (prev.state == "closed" and state == "open")):
            content_updated_at = raw.get("updated_at")
    return {
        "title": title,
        "body": body,
        "state": state,
        "state_reason": raw.get("state_reason"),
        "author": raw.get("author", ""),
        "author_association": raw.get("author_association"),
        "assignees": raw.get("assignees") or [],
        "labels": raw.get("labels") or [],
        "comments": raw.get("comments", 0),
        "reactions_total": raw.get("reactions_total", 0),
        "thumbs_up": raw.get("thumbs_up", 0),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "last_edited_at": last_edited_at,
        "content_updated_at": content_updated_at,
        "url": f"https://github.com/{config.repo()}/issues/{raw['number']}",
    }


def _facts_unchanged(iss: issue_model.Issue, meta: dict, summary: dict,
                     repro: dict, github: list[dict] | None) -> bool:
    """True when re-ingesting reproduces identical meta/summary/repro and the same
    GitHub closing references, so the write is a no-op. `meta` is the section that
    would be written, so a fact a partial raw cannot see reads as unchanged; a
    None `github` — unknown — reads the same way. The candidate links are NOT
    compared: the existing snapshot omits the (large) candidate arrays to keep the
    corpus load off Supabase's statement timeout, so an unchanged issue keeps its
    stored candidates until its other facts move. Ignores the checked_at /
    against_updated_at stamps."""
    rec = iss.rec

    def same(section: str, new: dict) -> bool:
        stored = rec.get(section)
        return stored is not None and all(stored.get(k) == v for k, v in new.items())

    if github is not None and (rec.get("links") or {}).get("github", []) != github:
        return False
    return same("meta", meta) and same("summary", summary) and same("repro", repro)


def _swap_facts(store: IssueStore, raw: dict, summary: dict, repro: dict,
                links: list[dict] | None, github: list[dict] | None) -> SwapResult:
    """Write `raw`'s facts over issue `raw['number']` as the store holds it now:
    re-read the record, stage the facts onto it, and swap it in while its
    write-stamp still matches, so a section another writer saved during the run
    stands. Each attempt re-reads, so a retry stages onto the winner's record."""
    n = int(raw["number"])
    for _ in range(SWAP_ATTEMPTS):
        got = store.stamped_issue(n)
        if got is None:
            return "absent"
        iss, stamp = got
        iss.stage_facts(_meta(raw, iss), summary=summary, repro=repro, links=links,
                        github=github)
        if store.save_issue_if(iss, stamp):
            return "written"
    return "lost"


def ingest_records(store: IssueStore, raws: list[dict],
                   prs: list[dict] | None) -> IngestCounts:
    """Upsert each normalized issue raw whose facts changed: meta + summary +
    repro + links. meta/summary/repro are computed for every raw (cheap) and
    compared, along with the raw's GitHub closing references, to the store; only
    issues that differ are written, and the candidate links are recomputed only
    for those — so an unchanged re-ingest skips the per-issue upsert, which is
    the loop's dominant cost against a networked store. With `prs` None the
    candidate links are not computed and each issue keeps the ones it has. The comparison reads a
    corpus loaded once WITHOUT its candidate arrays (loading them all
    intermittently exceeds the store's statement timeout); each changed issue is
    then re-read in full and written over the record it read, under a
    compare-and-swap on that record's write-stamp, so a section another writer
    saved mid-run survives. An issue that loses every one of its swap attempts is
    skipped and counted in `swap_lost`, leaving the rest of the batch to land —
    the write is idempotent, so the next run recomputes it. Every read and write
    shares one reused connection (store.batch). A rewritten issue's summary and
    repro are re-stamped against its updated_at, and its meta carries the content
    time issue_freshness reads the other facts against. Where `progress` reports, the
    comparison and the writes are reported on stdout."""
    if not raws:
        return IngestCounts(0, 0)
    report = progress.enabled()
    if report:
        progress.say(f"  loading stored issues to compare the {len(raws):,} fetched ones against…")
    existing = store.all_issues(omit_candidates=True)
    refs = link_prs.parse_refs(prs) if prs is not None else None  # each body parsed once
    # (raw, meta, summary, repro) for each raw whose facts differ from the store's.
    changed: list[tuple[dict, dict, dict, dict]] = []
    for raw in raws:
        n = raw["number"]
        prev = existing.get(n)
        meta = _meta(raw, prev)
        s = summarize_issues.summarize({"number": n, "title": meta["title"], "body": meta["body"]})
        summary = {"subsystem": s["subsystem"], "identifiers": s["identifiers"]}
        repro = repro_grade.grade_repro(meta["body"])
        if prev is None or not _facts_unchanged(prev, meta, summary, repro,
                                                raw.get("github_links")):
            changed.append((raw, meta, summary, repro))
    if report:
        progress.say(f"  {len(changed):,} of {len(raws):,} issues new or changed")
    saving = (progress.Progress("linking and saving", len(changed), "new or changed issues",
                                one="new or changed issue") if report else None)
    written = 0
    swap_lost = 0
    with store.batch():
        for raw, meta, summary, repro in changed:
            n = raw["number"]
            github = raw.get("github_links")
            links = None if prs is None else link_prs.candidate_prs(
                n, summary["subsystem"], prs, refs, issue_text=f"{meta['title']}\n{meta['body']}")
            # The swap runs even for an issue the snapshot lacks: another machine
            # may have created and sectioned it since, and the create must not
            # overwrite that record whole.
            swapped = _swap_facts(store, raw, summary, repro, links, github)
            if swapped == "absent":
                issue_model.Issue(store, {"issue": int(n)}).apply_facts(
                    meta, summary=summary, repro=repro, links=links, github=github)
            if swapped == "lost":
                swap_lost += 1
            else:
                written += 1
            if saving is not None:
                saving.advance()
    if saving is not None:
        saving.finish(f"{written:,} written, {swap_lost:,} contended")
    return IngestCounts(written, swap_lost)


def reconcile_closures(store: IssueStore, open_now: set[int], prs: list[dict],
                       fetch_one: Callable[[int], dict | None] = fetch_issues.fetch_issue,
                       ) -> int:
    """Issues in the store still marked open but absent from the open fetch have
    closed upstream (or fell past fetch_all's pagination cap); refetch each one
    and upsert it through ingest_records, so its meta AND derived facts (summary,
    repro, candidate links) match the refetched content. The refetch is a partial
    view, and the facts it cannot see keep their stored values. An unfetchable
    issue is left untouched. Returns how many issues changed state. Where
    `progress` reports, the refetches are counted on stdout."""
    report = progress.enabled()
    if report:
        progress.say("  loading stored issues to find open ones the fetch did not return…")
    missing = [(n, iss) for n, iss in store.all_issues(omit_candidates=True).items()
               if n not in open_now and iss.state == "open"]
    if report and not missing:
        progress.say("  every issue the store holds open was in the open fetch")
    refetching = (progress.Progress("refetching", len(missing),
                                    "stored-open issues missing from the open fetch",
                                    one="stored-open issue missing from the open fetch")
                  if report else None)
    refetched: list[dict] = []
    transitions = 0
    for n, iss in missing:
        raw = fetch_one(n)
        if refetching is not None:
            refetching.advance()
        if raw is None:
            continue
        if raw.get("state", "open") != iss.state:
            transitions += 1
        refetched.append(raw)
    if refetching is not None:
        refetching.finish(f"{transitions:,} changed state, "
                          f"{len(missing) - len(refetched):,} could not be fetched")
    ingest_records(store, refetched, prs)
    return transitions


def _load_prs(pr_store: Store | None = None) -> list[dict]:
    """The PR corpus the issue<->PR linker matches against: every open or merged
    PR in the SQL PR store (a merged fixer stays linked — the durable evidence an
    issue is likely fixed), each carrying its `state` and tagged with the shared
    subsystem taxonomy (so subsystem-match candidates work). Read as the store's
    projected link rows — number, title, description and state — because the
    explicit `Fixes #N` parse reads the description and nothing else is needed."""
    from pipeline.store import Store
    store = pr_store or Store()
    return [{"number": r["number"], "title": r["title"], "body": r["body"],
             "state": r["state"],
             "subsystem": summarize_issues.classify_subsystem(r["title"], r["body"])}
            for r in store.link_rows() if r["state"] in pr_index.LINKING_STATES]


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
    clock = time.monotonic()
    print("fetching open issues…", flush=True)
    raws = fetch_issues.fetch_all(max_issues=args.max)
    print(f"  {len(raws):,} open issues fetched in "
          f"{progress.duration(time.monotonic() - clock)}", flush=True)
    print("loading open and merged PRs to link issues against…", flush=True)
    step = time.monotonic()
    prs = _load_prs()
    print(f"  {len(prs):,} PRs loaded in {progress.duration(time.monotonic() - step)}",
          flush=True)
    print(f"comparing {len(raws):,} open issues with the store; saving the new and "
          f"changed ones, linked against {len(prs):,} PRs…", flush=True)
    counts = ingest_records(store, raws, prs)
    transitions = 0
    if args.max is None:
        print("reconciling closures…", flush=True)
        transitions = reconcile_closures(store, {r["number"] for r in raws}, prs)
    phase = "ingest" if args.max is None else "ingest:smoke"
    # swap_lost counts the open-fetch pass's contended issues; the closure sweep
    # skips its own the same way, and the next run recomputes every one of them.
    stats = {"open_fetched": len(raws), "upserted": counts.written,
             "swap_lost": counts.swap_lost, "state_transitions": transitions}
    store.append_run({"phase": phase, "started": started, "finished": _now(),
                      "stats": stats})
    dest = store.engine.url.host or store.root  # networked store: host; SQLite: local root
    print(f"ingested {len(raws)} open issues ({counts.written} changed, written, "
          f"{counts.swap_lost} contended), {transitions} state transitions -> {dest} "
          f"in {progress.duration(time.monotonic() - clock)}", flush=True)


if __name__ == "__main__":
    main()
