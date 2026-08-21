"""Analyze pending issues headlessly, in parallel batches: select open issues
whose analysis is missing or stale, bundle their evidence, run the canonical
issue-ANALYZE prompt through locked-down headless claudes (several batches at
once), and commit the verdicts back to the store. The agentic ANALYZE path —
run from the app Control tab (the `issue-analyze` job) or the CLI. Progress
prints one line per step; the app streams it as SSE.

Store I/O stays on the calling thread — the pending scan and bundle build happen
once up front, and each finished batch's verdicts are applied serially as it
returns — so the worker threads only ever run agents, never touch the store.

  uv run python issue_triage/analyze_issues.py [--limit N] [--batch N] [--concurrency N] [--store DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from issue_triage import issue_analyze_driver
from issue_triage.issue_store import IssueStore
from pipeline import settings
from pipeline import headless_agent
from pipeline import storekit
from pipeline.settings import REPO_ROOT

_print_lock = threading.Lock()


def _say(msg: str) -> None:
    # Worker threads and the main thread both print; the lock keeps lines whole.
    with _print_lock:
        print(msg, flush=True)


def _label(entries: list[dict]) -> str:
    nums = [e["number"] for e in entries]
    return f"#{nums[0]}–#{nums[-1]}" if len(nums) > 1 else f"#{nums[0]}"


def run_batch_agent(entries: list[dict]) -> list[dict]:
    """Run one headless analyze agent over a pre-built bundle slice and return its
    in-batch, valid verdicts. Pure with respect to the store — writes only a temp
    bundle file. Verdicts for issues outside the batch or with an unknown
    disposition are dropped (with a warning)."""
    label = _label(entries)

    def on_event(ev) -> None:
        if ev[0] == "tool":
            inp = ev[2] if len(ev) > 2 else {}
            _say(f"    [{label}] · {headless_agent.tool_summary(ev[1], inp)}")

    with tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="issue-analyze-", delete=False) as f:
        # indent=1: the agent's Read tool truncates very long lines, so each
        # field gets its own line.
        f.write(json.dumps(entries, indent=1))
        bundle_path = f.name
    prompt = (issue_analyze_driver.ANALYZE_PROMPT.replace("__BUNDLE_PATH__", bundle_path)
              .replace("__REPO__", settings.repo())
              + issue_analyze_driver.ANALYZE_FENCED_TAIL)
    text = headless_agent.run_agent(prompt, allow_gh=True, cwd=str(REPO_ROOT),
                                    on_event=on_event)
    verdicts = headless_agent.extract_json(text).get("verdicts") or []
    in_batch = {e["number"] for e in entries}
    good = [v for v in verdicts
            if int(v.get("issue", -1)) in in_batch
            and v.get("disposition") in issue_analyze_driver.VALID]
    if len(good) < len(verdicts):
        _say(f"    ! {label}: dropped {len(verdicts) - len(good)} verdict(s) — "
             "outside the batch or unknown disposition")
    missing = sorted(in_batch - {int(v["issue"]) for v in good})
    if missing:
        _say(f"    ! {label}: {len(missing)} issue(s) got no verdict: "
             f"{' '.join(f'#{n}' for n in missing[:10])}")
    return good


def analyze_batch(store: IssueStore, numbers: list[int]) -> int:
    """Bundle `numbers`, run one headless analyze agent over them, and apply its
    verdicts. Synchronous, single-batch convenience — the parallel `main` path
    keeps store I/O on its own thread. Returns how many verdicts were applied."""
    entries = issue_analyze_driver.bundle(
        store, only=numbers, pr_states=issue_analyze_driver.load_pr_states())
    good = run_batch_agent(entries)
    return issue_analyze_driver.apply_verdicts(store, good)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=100,
                    help="max issues to analyze this run (default 100)")
    ap.add_argument("--batch", type=int, default=25,
                    help="issues per agent call (default 25)")
    ap.add_argument("--concurrency", type=int, default=8,
                    help="agent calls to run at once (default 8)")
    ap.add_argument("--retries", type=int, default=2,
                    help="extra passes over batches that failed (default 2)")
    ap.add_argument("--store", type=Path, default=None,
                    help="issue store root (default: the shared store)")
    args = ap.parse_args(argv)
    started = storekit.now()
    store = IssueStore(args.store) if args.store else IssueStore()
    pend = issue_analyze_driver.pending(store)
    todo = pend[:args.limit]
    conc = max(1, args.concurrency)
    _say(f"① {len(pend)} issues to analyze; taking {len(todo)} this run "
         f"in batches of {args.batch}, up to {conc} at a time…")
    if not todo:
        _say("✓ nothing pending — analysis is current.")
        return 0
    # One store read up front; the workers see only their pre-built slice.
    entries = issue_analyze_driver.bundle(
        store, only=todo, pr_states=issue_analyze_driver.load_pr_states())
    pending_batches = [entries[i:i + args.batch] for i in range(0, len(entries), args.batch)]
    total = len(pending_batches)
    applied = 0
    for attempt in range(max(0, args.retries) + 1):
        if not pending_batches:
            break
        if attempt:
            _say(f"↻ retrying {len(pending_batches)} failed batch(es)…")
        failed: list[list[dict]] = []
        done = 0
        with ThreadPoolExecutor(max_workers=conc) as pool:
            futures = {pool.submit(run_batch_agent, b): b for b in pending_batches}
            for fut in as_completed(futures):
                label = _label(futures[fut])
                done += 1
                try:
                    good = fut.result()
                except Exception as e:
                    failed.append(futures[fut])
                    _say(f"    ! {label} failed: {e}  ({done}/{len(pending_batches)})")
                    continue
                # Serial on the main thread — the store is never touched concurrently.
                n = issue_analyze_driver.apply_verdicts(store, good)
                applied += n
                _say(f"    ✓ {label}: {n} verdicts applied  ({done}/{len(pending_batches)})")
        pending_batches = failed
    # Named from the store, so it covers every way an issue can be left behind:
    # an unrecovered batch, and a verdict the agent or the driver dropped.
    missed = sorted(set(todo) & set(issue_analyze_driver.pending(store)))
    remaining = len(issue_analyze_driver.pending(store))
    _say(f"✓ applied {applied} verdicts across {total - len(pending_batches)}/"
         f"{total} batches; {remaining} issues still pending analysis.")
    if missed:
        _say(f"    ! {len(missed)} of this run's issues have no current analysis: "
             + " ".join(f"#{n}" for n in missed[:20])
             + (" …" if len(missed) > 20 else ""))
    # A whole-run summary distinct from apply_verdicts' per-batch "analyze" entries
    # above: this one carries a real started/finished pair plus `attempted`, so
    # it's the sample a seconds-per-issue rate can be computed from.
    store.append_run({"phase": "analyze", "started": started, "finished": storekit.now(),
                      "stats": {"applied": applied,
                                "failed_batches": len(pending_batches),
                                "missed": len(missed), "attempted": len(todo)}})
    return 0 if applied else 1


if __name__ == "__main__":
    sys.exit(main())
