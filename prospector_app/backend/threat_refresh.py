"""Threat-scanning each head the store records.

INGEST and `ingest.refresh_prs` record a PR's new head without scanning the
code behind it, and the earlier head's `clear` stamp stays on the record until
a scan judges the new one. A worker machine scans on a cadence every open PR
`threat_scan.unscanned` names — a moved head, a new arrival, or a blocked
author's PR not yet reading malicious — through the same `threat_scan.scan`
the full Phase 0.5 run uses: it fetches the missing diffs, stamps each
verdict, blocks a malicious author and logs the incident. A pass that scans
anything is booked in the runs ledger under PHASE. The scan is deterministic
and runs no agent, so no capacity gate applies. `wake` starts a pass early,
for a caller in this process that has just recorded new heads.
"""
from __future__ import annotations

import threading
import time
import traceback

from pipeline import settings, storekit, threat_scan
from prospector_app.backend import data

REFRESH_SECONDS = 10 * 60
# PRs scanned per pass, most recently updated first; the rest wait a pass.
BATCH = 200
# How long a head a pass left without a verdict (no diff GitHub would give)
# waits before a pass tries it again.
RETRY_SECONDS = 6 * 3600
PHASE = "threat-scan:heads"

_thread: threading.Thread | None = None
_wake = threading.Event()
# (PR, head) → when a pass left that head without a verdict, by time.monotonic().
_left: dict[tuple[int, str], float] = {}


def candidates(now: float | None = None) -> list[int]:
    """The PRs `threat_scan.unscanned` names, less the heads a pass left
    without a verdict within RETRY_SECONDS, most recently updated first."""
    now = time.monotonic() if now is None else now
    for key, at in list(_left.items()):
        if now - at >= RETRY_SECONDS:
            del _left[key]
    snap = data.prs()
    picks = [n for n in threat_scan.unscanned(snap, data.store().load_threats())
             if (n, snap[n].head_sha or "") not in _left]
    return sorted(picks, key=lambda n: (snap[n].updated_at or "", n), reverse=True)


def scan_new_heads(limit: int = BATCH) -> list[int]:
    """Scan up to `limit` of the candidates. Returns the PRs scanned."""
    data.refresh()
    picks = candidates()[:limit]
    if not picks:
        return []
    snap = data.prs()
    prs = {n: snap[n] for n in picks}
    started = storekit.now()
    result = threat_scan.scan(data.store(), prs)
    left_at = time.monotonic()
    for n in result.unstamped:
        _left[(n, prs[n].head_sha or "")] = left_at
    data.store().append_run({
        "phase": PHASE, "started": started, "finished": storekit.now(),
        "trigger": "worker", "prs": sorted(picks), "stats": result.stats,
        "malicious_prs": sorted(result.malicious)})
    data.refresh()
    print(f"[threat-refresh] scanned {len(picks)} new head(s): "
          f"{sorted(picks)[:12]}{' …' if len(picks) > 12 else ''}"
          + (f"; MALICIOUS: {sorted(result.malicious)}" if result.malicious else ""),
          flush=True)
    return picks


def wake() -> None:
    _wake.set()


def _loop() -> None:
    while True:
        _wake.clear()
        try:
            scan_new_heads()
        except Exception:
            traceback.print_exc()
        _wake.wait(REFRESH_SECONDS)


def start() -> bool:
    """Start the cadence on a worker machine (one whose verify or fix lane is
    enabled). Idempotent."""
    global _thread
    if not (settings.verify_worker_enabled() or settings.fix_worker_enabled()):
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _thread = threading.Thread(target=_loop, daemon=True, name="threat-refresh")
    _thread.start()
    return True
