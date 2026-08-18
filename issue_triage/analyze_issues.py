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
              + issue_analyze_driver.ANALYZE_FENCED_TAIL)
    text = headless_agent.run_agent(prompt, allow_gh=False, cwd=str(REPO_ROOT),
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
    batches = [entries[i:i + args.batch] for i in range(0, len(entries), args.batch)]
    applied = 0
    failed_batches = 0
    done = 0
    with ThreadPoolExecutor(max_workers=conc) as pool:
        futures = {pool.submit(run_batch_agent, b): b for b in batches}
        for fut in as_completed(futures):
            label = _label(futures[fut])
            done += 1
            try:
                good = fut.result()
            except Exception as e:
                failed_batches += 1
                _say(f"    ! {label} failed, continuing: {e}  ({done}/{len(batches)})")
                continue
            # Serial on the main thread — the store is never touched concurrently.
            n = issue_analyze_driver.apply_verdicts(store, good)
            applied += n
            _say(f"    ✓ {label}: {n} verdicts applied  ({done}/{len(batches)})")
    remaining = len(issue_analyze_driver.pending(store))
    _say(f"✓ applied {applied} verdicts across {len(batches) - failed_batches}/"
         f"{len(batches)} batches; {remaining} issues still pending analysis.")
    # A whole-run summary distinct from apply_verdicts' per-batch "analyze" entries
    # above: this one carries a real started/finished pair plus `attempted`, so
    # it's the sample a seconds-per-issue rate can be computed from.
    store.append_run({"phase": "analyze", "started": started, "finished": storekit.now(),
                      "stats": {"applied": applied, "failed_batches": failed_batches,
                                "attempted": len(todo)}})
    return 0 if applied else 1


if __name__ == "__main__":
    sys.exit(main())
