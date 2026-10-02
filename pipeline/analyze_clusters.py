"""Analyze pending clusters headlessly, in parallel: select clusters with no
outcome yet or with any active member whose analysis is missing/stale, hand
each its own evidence bundle, run the canonical per-cluster ANALYZE prompt
through locked-down headless claudes (several at once), and commit the
verdicts back to the store. The agentic bulk ANALYZE path — run from the
app Control tab (the `analyze-clusters` job) or the CLI, as the backlog
counterpart to `triage_cluster.py` (which triages one cluster end-to-end,
including a GitHub refresh). Progress prints one line per step; the app
streams it as SSE.

Store I/O stays on the calling thread — bundles are built once up front (over
one shared redundancy-tree cache), and each finished cluster's payload is
committed serially as it returns — so the worker threads only ever run
agents, never touch the store.

  uv run python pipeline/analyze_clusters.py [--limit N] [--concurrency N] [--store DIR]
"""
from __future__ import annotations

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from pipeline import agent_wave
from pipeline import analyze_driver
from pipeline import headless_agent
from pipeline import progress
from pipeline import redundancy
from pipeline.store import Store
from pipeline.storekit import now as _now

_print_lock = threading.Lock()


def _say(msg: str) -> None:
    # Worker threads and the main thread both print; the lock keeps lines whole.
    with _print_lock:
        print(msg, flush=True)


def run_cluster_agent(cid: int, bundle: dict) -> dict:
    """Run one headless analyze agent over a pre-built cluster bundle and
    return its parsed payload. Pure with respect to the store. Raises ValueError if the agent's reply carries no
    parseable JSON."""

    def on_event(ev) -> None:
        if ev[0] == "tool":
            inp = ev[2] if len(ev) > 2 else {}
            _say(f"    [cluster {cid}] · {headless_agent.tool_summary(ev[1], inp)}")

    return headless_agent.extract_json(analyze_driver.run_analyze_agent(bundle, on_event))


@dataclass(frozen=True)
class AnalyzeRun:
    attempted: int
    committed: int
    failed: int
    # Agents that started and raised: a count equal to `attempted` is a run
    # whose every agent failed.
    errored: int
    stop: agent_wave.Stop | None


def run(store: Store, *, limit: int, concurrency: int,
        trigger: str | None = None) -> AnalyzeRun:
    """Analyze up to `limit` pending clusters, `concurrency` agents at a time,
    committing each verdict as it returns, and book the run in the ledger
    (stamped with `trigger` when one is given)."""
    started = _now()
    _say("① Finding the clusters that need analysis (reads every PR and cluster "
         "from the store)…")
    pend = analyze_driver.pending(store)
    todo = pend[:limit]
    conc = max(1, concurrency)
    _say(f"  {len(pend)} clusters to analyze; taking {len(todo)} this run, "
         f"up to {conc} at a time.")
    if not todo:
        _say("✓ nothing pending — analysis is current.")
        return AnalyzeRun(0, 0, 0, 0, None)

    # One store read + shared redundancy-tree cache up front; workers see only
    # their own pre-built bundle.
    _say("② Building each cluster's evidence bundle (reads every PR from the store, "
         "then each changed file on the default branch from GitHub)…")
    prs = store.all_prs()
    master = redundancy.MasterTree()
    bundles: dict[int, dict] = {}
    building = progress.Progress("bundling", len(todo), "clusters", one="cluster")
    for c in todo:
        b = analyze_driver.bundle(store, c.id, prs, master=master)
        if b is not None:
            bundles[c.id] = b
        building.advance()
    building.finish()

    committed = 0
    failed = 0
    errored = 0
    done = 0
    stop: agent_wave.Stop | None = None
    _say(f"③ Running {len(bundles)} analyze agent(s), up to {conc} at a time "
         f"(each can take several minutes)…")
    wave = agent_wave.Wave()
    with ThreadPoolExecutor(max_workers=conc) as pool:
        futures = {wave.submit(pool, run_cluster_agent, cid, b): cid
                   for cid, b in bundles.items()}
        for fut in as_completed(futures):
            cid = futures[fut]
            done += 1
            try:
                payload = fut.result()
            except agent_wave.NotStarted:
                failed += 1
                continue
            except Exception as e:
                failed += 1
                errored += 1
                _say(f"    ! cluster {cid} failed, continuing: {e}  ({done}/{len(todo)})")
                if stop is None and (stop := agent_wave.stop_reason(e, wave.not_started())):
                    _say(stop.line)
                continue
            # Serial on the main thread — the store is never touched concurrently.
            errs = analyze_driver.commit_analysis(store, payload)
            if errs:
                failed += 1
                _say(f"    ! cluster {cid}: validation failed  ({done}/{len(todo)})")
                for e in errs:
                    _say(f"        {e}")
            else:
                committed += 1
                _say(f"    ✓ cluster {cid} committed  ({done}/{len(todo)})")

    _say("④ Wrapping up — each step reads every PR from the store:")
    _say("  dispositioning standalone PRs…")
    orphans = analyze_driver.disposition_orphans(store)
    _say("  refreshing salvage-fix action items…")
    salvage = analyze_driver.backfill_salvage_items(store, today=_now()[:10])
    _say("  counting the clusters still pending (reads the clusters too)…")
    remaining = len(analyze_driver.pending(store))
    _say(f"✓ committed {committed}/{len(todo)} clusters ({failed} failed); "
         f"{orphans} standalone PR(s) dispositioned; {salvage} salvage-fix item(s); "
         f"{remaining} clusters still pending analysis.")
    record: dict = {"phase": "analyze:commit", "started": started, "finished": _now(),
                    "stats": {"committed": committed, "failed": failed,
                              "orphans": orphans, "salvage_items": salvage,
                              "attempted": len(todo)}}
    if trigger:
        record["trigger"] = trigger
    store.append_run(record)
    return AnalyzeRun(len(todo), committed, failed, errored, stop)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=20,
                    help="max clusters to analyze this run (default 20)")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="agent calls to run at once (default 4)")
    ap.add_argument("--store", type=Path, default=None,
                    help="store root (default: the shared store)")
    args = ap.parse_args(argv)
    store = Store(args.store) if args.store else Store()
    result = run(store, limit=args.limit, concurrency=args.concurrency)
    if result.stop:
        return result.stop.exit_code
    return 0 if result.committed or not result.attempted else 1


if __name__ == "__main__":
    sys.exit(main())
