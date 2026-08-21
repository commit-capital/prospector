"""Scan candidate alerts for an already-landed fix, headlessly and in parallel
waves: apply the deterministic tier-0 verdicts first, then bundle the remaining
open, unscanned alerts (highest severity first), run the canonical FIND-FIXED
prompt through gh-enabled headless claudes (several batches at once), and
commit the verdicts back to the store. Run from the app Control tab (the
`security-sweep` job) or the CLI. Progress prints one line per step; the app
streams it as SSE.

Store I/O stays on the calling thread — the candidate scan and bundle build
happen once up front, and each finished batch's verdicts are applied serially
as it returns — so a stop/abort mid-run keeps every committed batch, and the
worker threads only ever run agents.

  uv run python alert_triage/find_fixed.py [--limit N] [--batch N] [--concurrency N] [--store DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from alert_triage import alert_fixed_driver
from alert_triage import config
from alert_triage.alert_store import AlertStore
from pipeline import headless_agent
from pipeline.settings import REPO_ROOT

_print_lock = threading.Lock()


def _say(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _label(entries: list[dict]) -> str:
    tags = [f"{e['source']}#{e['number']}" for e in entries]
    return f"{tags[0]}–{tags[-1]}" if len(tags) > 1 else tags[0]


def _path_prober(token: str) -> Callable[[str], bool]:
    """A default-branch file-existence probe over the contents API. Treats any
    non-404 answer as "exists" so a transient error never fabricates a
    deleted-file verdict."""
    def exists(path: str) -> bool:
        try:
            config.gh_alert_read(f"repos/{config.REPO}/contents/{path}", token)
            return True
        except config.SourceUnavailable as e:
            return "404" not in e.detail
        except Exception:
            return True
    return exists


def run_batch_agent(entries: list[dict]) -> list[dict]:
    """Run one gh-enabled headless find-fixed agent over a pre-built bundle
    slice and return its in-batch, valid verdicts. Pure with respect to the
    store — writes only a temp bundle file."""
    label = _label(entries)

    def on_event(ev) -> None:
        if ev[0] == "tool":
            inp = ev[2] if len(ev) > 2 else {}
            _say(f"    [{label}] · {headless_agent.tool_summary(ev[1], inp)}")

    with tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="alert-find-fixed-", delete=False) as f:
        f.write(json.dumps(entries, indent=1))
        bundle_path = f.name
    prompt = (alert_fixed_driver.FIND_FIXED_PROMPT.replace("__BUNDLE_PATH__", bundle_path)
              + alert_fixed_driver.FIND_FIXED_FENCED_TAIL)
    text = headless_agent.run_agent(prompt, allow_gh=True, cwd=str(REPO_ROOT),
                                    on_event=on_event)
    verdicts = headless_agent.extract_json(text).get("verdicts") or []
    in_batch = {e["id"] for e in entries}
    good = [v for v in verdicts
            if int(v.get("id", -1)) in in_batch
            and v.get("verdict") in alert_fixed_driver.VALID]
    if len(good) < len(verdicts):
        _say(f"    ! {label}: dropped {len(verdicts) - len(good)} verdict(s) — "
             "outside the batch or unknown verdict")
    missing = sorted(in_batch - {int(v["id"]) for v in good})
    if missing:
        _say(f"    ! {label}: {len(missing)} alert(s) got no verdict")
    return good


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=12,
                    help="max alerts to scan this wave (default 12)")
    ap.add_argument("--batch", type=int, default=6,
                    help="alerts per agent call (default 6)")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="agent calls to run at once (default 4)")
    ap.add_argument("--store", type=Path, default=None,
                    help="alert store root (default: the shared store)")
    args = ap.parse_args(argv)
    store = AlertStore(args.store) if args.store else AlertStore()
    token = config.mint_token()
    tier0 = alert_fixed_driver.deterministic_fixed(
        store, path_exists=_path_prober(token) if token else None)
    if tier0:
        alert_fixed_driver.apply_verdicts(store, tier0)
        _say(f"⓪ tier-0: {len(tier0)} alert(s) resolved deterministically.")
    cands = alert_fixed_driver.candidates(store)
    todo = cands[:args.limit]
    conc = max(1, args.concurrency)
    _say(f"① {len(cands)} alerts to scan; taking {len(todo)} this wave "
         f"in batches of {args.batch}, up to {conc} at a time…")
    if not todo:
        _say("✓ nothing to scan — every open alert has a current fix-scan.")
        return 0
    entries = alert_fixed_driver.bundle(store, only=todo)
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
            n = alert_fixed_driver.apply_verdicts(store, good)
            applied += n
            _say(f"    ✓ {label}: {n} verdicts applied  ({done}/{len(batches)})")
    remaining = len(alert_fixed_driver.candidates(store))
    _say(f"✓ applied {applied} verdicts across {len(batches) - failed_batches}/"
         f"{len(batches)} batches; {remaining} alerts still unscanned.")
    return 0 if applied else 1


if __name__ == "__main__":
    sys.exit(main())
