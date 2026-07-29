"""Analyze pending clusters headlessly, in parallel: select clusters with no
outcome yet or with any active member whose analysis is missing/stale, hand
each its own evidence bundle, run the canonical per-cluster ANALYZE prompt
through locked-down headless claudes (several at once), and commit the
verdicts back to the store. The agentic bulk ANALYZE path — run from the
cockpit Control tab (the `analyze-clusters` job) or the CLI, as the backlog
counterpart to `triage_cluster.py` (which triages one cluster end-to-end,
including a GitHub refresh). Progress prints one line per step; the cockpit
streams it as SSE.

Store I/O stays on the calling thread — bundles are built once up front (over
one shared redundancy-tree cache), and each finished cluster's payload is
committed serially as it returns — so the worker threads only ever run
agents, never touch the store.

  uv run python pipeline/analyze_clusters.py [--limit N] [--concurrency N] [--store DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pipeline import analyze_driver
from pipeline import headless_agent
from pipeline import redundancy
from pipeline import settings
from pipeline.analyze_driver import ANALYZE_FENCED_TAIL, ANALYZE_PROMPT
from pipeline.settings import REPO_ROOT
from pipeline.store import Store
from pipeline.storekit import now as _now

_print_lock = threading.Lock()


def _say(msg: str) -> None:
    # Worker threads and the main thread both print; the lock keeps lines whole.
    with _print_lock:
        print(msg, flush=True)


def run_cluster_agent(cid: int, bundle: dict) -> dict:
    """Run one headless analyze agent over a pre-built cluster bundle and
    return its parsed payload. Pure with respect to the store — writes only a
    temp bundle file. Raises ValueError if the agent's reply carries no
    parseable JSON."""

    def on_event(ev) -> None:
        if ev[0] == "tool":
            inp = ev[2] if len(ev) > 2 else {}
            _say(f"    [cluster {cid}] · {headless_agent.tool_summary(ev[1], inp)}")

    with tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix=f"analyze-cluster-{cid}-", delete=False) as f:
        f.write(json.dumps(bundle, indent=1))
        bundle_path = f.name
    prompt = (ANALYZE_PROMPT.replace("__BUNDLE_PATH__", bundle_path)
                            .replace("__BRANCH__", settings.default_branch())
              + ANALYZE_FENCED_TAIL)
    text = headless_agent.run_agent(prompt, allow_gh=True, cwd=str(REPO_ROOT),
                                    on_event=on_event)
    return headless_agent.extract_json(text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=20,
                    help="max clusters to analyze this run (default 20)")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="agent calls to run at once (default 4)")
    ap.add_argument("--store", type=Path, default=None,
                    help="store root (default: the shared store)")
    args = ap.parse_args(argv)
    started = _now()
    store = Store(args.store) if args.store else Store()
    pend = analyze_driver.pending(store)
    todo = pend[:args.limit]
    conc = max(1, args.concurrency)
    _say(f"① {len(pend)} clusters to analyze; taking {len(todo)} this run, "
         f"up to {conc} at a time…")
    if not todo:
        _say("✓ nothing pending — analysis is current.")
        return 0

    # One store read + shared redundancy-tree cache up front; workers see only
    # their own pre-built bundle.
    prs = store.all_prs()
    master = redundancy.MasterTree()
    bundles: dict[int, dict] = {}
    for c in todo:
        b = analyze_driver.bundle(store, c.id, prs, master=master)
        if b is not None:
            bundles[c.id] = b

    committed = 0
    failed = 0
    done = 0
    with ThreadPoolExecutor(max_workers=conc) as pool:
        futures = {pool.submit(run_cluster_agent, cid, b): cid for cid, b in bundles.items()}
        for fut in as_completed(futures):
            cid = futures[fut]
            done += 1
            try:
                payload = fut.result()
            except Exception as e:
                failed += 1
                _say(f"    ! cluster {cid} failed, continuing: {e}  ({done}/{len(todo)})")
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

    orphans = analyze_driver.disposition_orphans(store)
    salvage = analyze_driver.backfill_salvage_items(store, today=_now()[:10])
    remaining = len(analyze_driver.pending(store))
    _say(f"✓ committed {committed}/{len(todo)} clusters ({failed} failed); "
         f"{orphans} standalone PR(s) dispositioned; {salvage} salvage-fix item(s); "
         f"{remaining} clusters still pending analysis.")
    store.append_run({"phase": "analyze:commit", "started": started, "finished": _now(),
                      "stats": {"committed": committed, "failed": failed,
                                "orphans": orphans, "salvage_items": salvage,
                                "attempted": len(todo)}})
    return 0 if committed else 1


if __name__ == "__main__":
    sys.exit(main())
