"""Scan candidate issues for an already-landed fix, headlessly and in parallel
waves: select open, unscanned issues (highest pain first), bundle their symptom
evidence, run the canonical FIND-FIXED prompt through gh-enabled headless claudes
(several batches at once), and commit the verdicts back to the store. The agentic
FIND-FIXED path — run from the app Control tab (the `issue-find-fixed` job) or
the CLI. Progress prints one line per step; the app streams it as SSE.

Store I/O stays on the calling thread — the candidate scan and bundle build happen
once up front, and each finished batch's verdicts are applied serially as it
returns — so a stop/abort mid-run keeps every committed batch, and the worker
threads only ever run agents.

  uv run python issue_triage/find_fixed.py [--limit N] [--batch N] [--concurrency N] [--store DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from issue_triage import issue_fixed_driver
from issue_triage.issue_store import IssueStore
from pipeline import settings
from pipeline import headless_agent
from pipeline.settings import REPO_ROOT

_print_lock = threading.Lock()


def _say(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _label(entries: list[dict]) -> str:
    nums = [e["number"] for e in entries]
    return f"#{nums[0]}–#{nums[-1]}" if len(nums) > 1 else f"#{nums[0]}"


def run_batch_agent(entries: list[dict]) -> list[dict]:
    """Run one gh-enabled headless find-fixed agent over a pre-built bundle slice
    and return its in-batch, valid verdicts. Pure with respect to the store —
    writes only a temp bundle file."""
    label = _label(entries)

    def on_event(ev) -> None:
        if ev[0] == "tool":
            inp = ev[2] if len(ev) > 2 else {}
            _say(f"    [{label}] · {headless_agent.tool_summary(ev[1], inp)}")

    with tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="find-fixed-", delete=False) as f:
        f.write(json.dumps(entries, indent=1))
        bundle_path = f.name
    prompt = (issue_fixed_driver.FIND_FIXED_PROMPT.replace("__BUNDLE_PATH__", bundle_path)
              .replace("__REPO__", settings.repo())
              + issue_fixed_driver.FIND_FIXED_FENCED_TAIL)
    text = headless_agent.run_agent(prompt, allow_gh=True, cwd=str(REPO_ROOT),
                                    on_event=on_event)
    verdicts = headless_agent.extract_json(text).get("verdicts") or []
    in_batch = {e["number"] for e in entries}
    good = [v for v in verdicts
            if int(v.get("issue", -1)) in in_batch
            and issue_fixed_driver.verdict_error(v) is None]
    if len(good) < len(verdicts):
        _say(f"    ! {label}: dropped {len(verdicts) - len(good)} verdict(s) — "
             "outside the batch or invalid evidence")
    missing = sorted(in_batch - {int(v["issue"]) for v in good})
    if missing:
        _say(f"    ! {label}: {len(missing)} issue(s) got no verdict: "
             f"{' '.join(f'#{n}' for n in missing[:10])}")
    return good


def scan_batch(store: IssueStore, numbers: list[int]) -> int:
    """Bundle `numbers`, run one gh-enabled agent over them, and apply its
    verdicts. Synchronous, single-batch convenience. Returns verdicts applied."""
    entries = issue_fixed_driver.bundle(store, only=numbers)
    good = run_batch_agent(entries)
    return issue_fixed_driver.apply_verdicts(store, good)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=12,
                    help="max issues to scan this wave (default 12)")
    ap.add_argument("--batch", type=int, default=6,
                    help="issues per agent call (default 6)")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="agent calls to run at once (default 4)")
    ap.add_argument("--retries", type=int, default=2,
                    help="extra passes over batches that failed (default 2)")
    ap.add_argument("--store", type=Path, default=None,
                    help="issue store root (default: the shared store)")
    args = ap.parse_args(argv)
    store = IssueStore(args.store) if args.store else IssueStore()
    cands = issue_fixed_driver.candidates(store)
    todo = cands[:args.limit]
    conc = max(1, args.concurrency)
    _say(f"① {len(cands)} issues to scan; taking {len(todo)} this wave "
         f"in batches of {args.batch}, up to {conc} at a time…")
    if not todo:
        _say("✓ nothing to scan — every open issue has a current fix-scan.")
        return 0
    entries = issue_fixed_driver.bundle(store, only=todo)
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
                n = issue_fixed_driver.apply_verdicts(store, good)
                applied += n
                _say(f"    ✓ {label}: {n} verdicts applied  ({done}/{len(pending_batches)})")
        pending_batches = failed
    # Named from the store, so it covers every way an issue can be left behind:
    # an unrecovered batch, and a verdict the agent or the driver dropped.
    missed = sorted(set(todo) & set(issue_fixed_driver.candidates(store)))
    remaining = len(issue_fixed_driver.candidates(store))
    _say(f"✓ applied {applied} verdicts across {total - len(pending_batches)}/"
         f"{total} batches; {remaining} issues still unscanned.")
    if missed:
        _say(f"    ! {len(missed)} of this run's issues have no current fix-scan: "
             + " ".join(f"#{n}" for n in missed[:20])
             + (" …" if len(missed) > 20 else ""))
    return 0 if applied else 1


if __name__ == "__main__":
    sys.exit(main())
