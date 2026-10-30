"""Closing contributors' PRs whose authors went quiet on the feedback holding
them, as won't-fix, with a note inviting them back.

The decision is `gates.abandoned_close`: blocking feedback on the PR's newest
commit, newer than that commit, and two weeks of its author's silence since.
The store narrows the PRs to read (`candidates`). Each is then read live from
GitHub (`handback.live_state`) for its head, newest commit and its author's
last activity. A worker machine runs a pass every INTERVAL_SECONDS under a
lease, closing at most `settings.abandoned_close_daily()` a UTC day through
`executor.execute_pr` (comment, then close, as the bot, Activity-logged).

Each close, dry-run or failure is one runs-ledger row (PHASE), so a head is
acted on once per mode: a PR someone reopens at the same head stays open, and a
failed close waits for an operator. A live pass runs only where the bot can
write (`executor.live_possible`). `TRIAGE_ABANDONED_CLOSE`
(`settings.abandoned_close`) is `off` (the default), `dry-run` or `live`.

    uv run prospector abandoned-close     # read-only: what a pass would close, and why
"""
from __future__ import annotations

import argparse
import json
import threading
import traceback
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pipeline import gates, settings, storekit
from prospector_app.backend import (data, executor, handback, models, profile_gate, schema_gate,
                                    worker_loops)

if TYPE_CHECKING:
    from pipeline.model import Pr

PHASE = "abandoned-close"
INTERVAL_SECONDS = 6 * 3600
LEASE = "abandoned-close"
LEASE_SECONDS = 2 * 3600

_thread: threading.Thread | None = None
_stop = threading.Event()


def marker(head: str) -> str:
    return f"<!-- prospector:abandoned-close:{head[:12]} -->"


def close_body(author: str | None, feedback: list[str], head: str) -> str:
    who = f"@{author}" if author else "there"
    items = "".join(f"\n- {w}" for w in feedback)
    return (f"Hi {who} — we're closing this PR for now, since it has been waiting on changes "
            f"from you for over two weeks. Its latest commit, `{head[:7]}`, still has:{items}\n\n"
            "Thanks for the contribution. If you'd like to pick it back up, push a commit that "
            "addresses the feedback, then comment here and we'll reopen it (or open a new PR "
            "with the changes). Either way, it goes straight back into review.\n\n"
            f"{marker(head)}")


def candidates(prs: dict[int, Pr], now: datetime) -> list[Pr]:
    """The open contributors' PRs with blocking feedback at their head old
    enough to close on, the longest-waiting first. Live facts decide the rest."""
    quiet = timedelta(days=gates.ABANDONED_QUIET_DAYS)
    out: list[tuple[datetime, Pr]] = []
    for pr in prs.values():
        if pr.state != "open" or gates.abandon_exempt(pr):
            continue
        ripe = [f.at for f in gates.head_feedback(pr) if now - f.at >= quiet]
        if ripe:
            out.append((min(ripe), pr))
    return [pr for _, pr in sorted(out, key=lambda c: (c[0], c[1].number))]


def judge(pr: Pr, now: datetime) -> tuple[bool, str, list[str]]:
    """Whether `pr` closes now, why, and the feedback its close names."""
    live = handback.live_state(pr.number)
    if live is None:
        return False, "GitHub did not answer", []
    if live["state"] != "open":
        return False, f"the PR is {live['state']}", []
    facts = gates.LivePr(head=live["head"], head_committed=live["head_committed"],
                         updated_at=live["updated_at"], last_active=live["last_active"],
                         ci_state=live["head_ci"])
    ok, why = gates.abandoned_close(pr, facts, now)
    return ok, why, [f.what for f in gates.current_feedback(pr, facts)] if ok else []


def _acted(mode: str, prs: list[int]) -> tuple[set[tuple[int, str]], int]:
    """Which of `prs` were already acted on in `mode`, as (PR, head) pairs,
    and how many acts in `mode` fell in the current UTC day."""
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    def rows(*, prs: list[int] | None = None, since: str | None = None) -> list[dict]:
        return [run.raw for run in data.store().runs(phase=PHASE, prs=prs, since=since)
                if isinstance(run, storekit.PhaseRun)
                and (run.raw.get("stats") or {}).get("mode") == mode]

    heads = {(int(r.get("pr") or 0), str((r.get("stats") or {}).get("head_sha")))
             for r in rows(prs=prs)}
    return heads, len(rows(since=midnight.isoformat()))


def close(pr: Pr, feedback: list[str], why: str, mode: str) -> str | None:
    """Close `pr` through the executor in `mode` and book it. Returns the
    booked step, or None when nothing was acted on: the executor found the PR
    moved or closed, or a live close fell back to a dry-run."""
    head = str(pr.head_sha or "")
    res = executor.execute_pr(
        pr.number, models.CloseAction(action="CLOSE_WONTFIX",
                                      comment=close_body(pr.author, feedback, head),
                                      reason=f"abandoned: {why}"),
        dry_run=mode != "live")
    status = str(res.get("status"))
    step = {"executed": "closed", "error": "failed"}.get(status)
    if status == "dry-run" and mode == "dry-run":
        step = "dry-run"
    if step is None:
        return None
    now = storekit.now()
    data.store().append_run({"phase": PHASE, "pr": pr.number, "started": now, "finished": now,
                             "trigger": "worker",
                             "stats": {"step": step, "mode": mode, "head_sha": head,
                                       "status": status, "why": why,
                                       "detail": res.get("detail")}})
    return step


def sweep_once(now: datetime | None = None) -> list[int] | None:
    """One pass: the PRs acted on, or None when the lane is off, a live pass
    cannot write as the bot, a pass ran too recently, or another machine holds
    the lease."""
    mode = settings.abandoned_close()
    if mode == "off" or (mode == "live" and not executor.live_possible()):
        return None
    now = now or datetime.now(timezone.utc)
    last = data.latest_run(f"{PHASE}:pass")
    if isinstance(last, storekit.PhaseRun):
        age = storekit.seconds_since(last.finished or last.started, now)
        if age is not None and age < INTERVAL_SECONDS:
            return None
    host = settings.worker_id()
    store = data.store()
    if not store.claim_lease(LEASE, host=host, seconds=LEASE_SECONDS):
        return None
    acted: list[int] = []
    try:
        started = storekit.now()
        picks = candidates(data.prs(), now)
        heads, today = _acted(mode, [pr.number for pr in picks])
        room = settings.abandoned_close_daily() - today
        for pr in picks:
            if room <= 0:
                break
            if (pr.number, str(pr.head_sha)) in heads:
                continue
            ok, why, feedback = judge(pr, now)
            if ok and close(pr, feedback, why, mode) is not None:
                acted.append(pr.number)
                room -= 1
        store.append_run({"phase": f"{PHASE}:pass", "started": started,
                          "finished": storekit.now(), "trigger": "worker",
                          "stats": {"mode": mode, "acted": acted}})
    finally:
        store.release_lease(LEASE, host=host)
    return acted


def enabled() -> bool:
    """Whether this machine runs the lane: a verify or fix worker with it on."""
    return settings.abandoned_close() != "off" and (settings.verify_worker_enabled()
                                                    or settings.fix_worker_enabled())


def _loop() -> None:
    while not _stop.is_set():
        try:
            if not (schema_gate.paused(PHASE) or profile_gate.paused(PHASE)):
                sweep_once()
        except Exception:
            traceback.print_exc()
        worker_loops.ran(PHASE, INTERVAL_SECONDS)
        _stop.wait(INTERVAL_SECONDS)


def start() -> bool:
    """Start the cadence on a machine that runs the lane (`enabled`). Idempotent."""
    global _thread
    if not enabled():
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name=PHASE)
    _thread.start()
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="prospector abandoned-close",
        description="List the PRs a pass would close as abandoned, and why. Reads the store "
                    "and GitHub; writes nothing.")
    p.add_argument("--json", help="also write every candidate's verdict to this file")
    args = p.parse_args(argv)
    now = datetime.now(timezone.utc)
    rows = []
    for pr in candidates(data.prs(), now):
        ok, why, feedback = judge(pr, now)
        rows.append({"pr": pr.number, "author": pr.author, "close": ok, "why": why,
                     "feedback": feedback})
        print(f"#{pr.number:<6} {'close' if ok else 'keep':<5} @{pr.author}  {why}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(rows, f, indent=1)
    closes = sum(r["close"] for r in rows)
    print(json.dumps({"candidates": len(rows), "close": closes, "keep": len(rows) - closes,
                      "daily_cap": settings.abandoned_close_daily(),
                      "mode": settings.abandoned_close()}))
    return 0
