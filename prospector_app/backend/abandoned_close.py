"""Closing contributors' PRs whose authors went quiet on the feedback holding
them, as won't-fix, with a note inviting them back.

The decision is `gates.abandoned_close`: blocking feedback on the PR's newest
commit, newer than that commit, and two weeks of its author's silence since.
The store narrows the PRs to read (`candidates`). Each is then read live from
GitHub (`handback.live_state`) for its head, newest commit and its author's
last activity. A worker machine runs a pass every INTERVAL_SECONDS under a
lease, acting on at most `settings.abandoned_close_daily()` PRs a UTC day.

A PR held only by CI that ran on an old base is brought current first
(`bring_current`), sorted as the backlog sweep sorts it: a clean or
agent-resolvable drift queues an autofix update or rebase, after which CI on
the new head is the author's to answer; one no request can bring current gets
our rebase ask, which is blocking feedback in its own right.

Each close, queued request, ask, dry-run or failure is one runs-ledger row
(PHASE), and each counts toward the daily cap. A live pass runs only where the bot can
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
from typing import TYPE_CHECKING, NamedTuple

from pipeline import gates, settings, storekit
from prospector_app.backend import (backlog_sweep, data, executor, fix_queue, handback, models,
                                    profile_gate, schema_gate, worker_loops)

if TYPE_CHECKING:
    from pipeline.model import Pr

PHASE = "abandoned-close"
INTERVAL_SECONDS = 6 * 3600
LEASE = "abandoned-close"
LEASE_SECONDS = 2 * 3600
# Steps that end the lane's work on a head in their mode: a PR someone reopens
# at the same head stays open, and a failed act waits for an operator.
FINAL_STEPS = {"closed", "dry-run", "failed"}

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
        ripe = [f.at for f in gates.abandon_feedback(pr) if now - f.at >= quiet]
        if ripe:
            out.append((min(ripe), pr))
    return [pr for _, pr in sorted(out, key=lambda c: (c[0], c[1].number))]


class Verdict(NamedTuple):
    close: bool
    update: bool  # held only by CI on an old base (gates.STALE_CI)
    why: str
    feedback: list[str]  # what the close names
    live: gates.LivePr | None


def _verdict(pr: Pr, facts: gates.LivePr, now: datetime) -> Verdict:
    ok, why = gates.abandoned_close(pr, facts, now)
    return Verdict(ok, why == gates.STALE_CI, why,
                   [f.what for f in gates.current_feedback(pr, facts)] if ok else [], facts)


def judge(pr: Pr, now: datetime) -> Verdict:
    """Whether `pr` closes now, and why, from its live state."""
    live = handback.live_state(pr.number)
    if live is None:
        return Verdict(False, False, "GitHub did not answer", [], None)
    if live["state"] != "open":
        return Verdict(False, False, f"the PR is {live['state']}", [], None)
    return _verdict(pr, gates.LivePr(head=live["head"], head_committed=live["head_committed"],
                                     updated_at=live["updated_at"],
                                     last_active=live["last_active"], ci_state=live["head_ci"]),
                    now)


def _acted(mode: str, prs: list[int]) -> tuple[dict[tuple[int, str], set[str]], int]:
    """The steps already taken on `prs` in `mode`, by (PR, head), and how many
    acts in `mode` fell in the current UTC day."""
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    def rows(*, prs: list[int] | None = None, since: str | None = None) -> list[dict]:
        return [run.raw for run in data.store().runs(phase=PHASE, prs=prs, since=since)
                if isinstance(run, storekit.PhaseRun)
                and (run.raw.get("stats") or {}).get("mode") == mode]

    heads: dict[tuple[int, str], set[str]] = {}
    for r in rows(prs=prs):
        stats = r.get("stats") or {}
        heads.setdefault((int(r.get("pr") or 0), str(stats.get("head_sha"))), set()).add(
            str(stats.get("step")))
    return heads, len(rows(since=midnight.isoformat()))


def _book(pr: Pr, step: str, mode: str, why: str, **stats: object) -> str:
    now = storekit.now()
    data.store().append_run({"phase": PHASE, "pr": pr.number, "started": now, "finished": now,
                             "trigger": "worker",
                             "stats": {"step": step, "mode": mode, "head_sha": str(pr.head_sha or ""),
                                       "why": why, **stats}})
    return step


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
    return _book(pr, step, mode, why, status=status, detail=res.get("detail"))


def bring_current(pr: Pr, verdict: Verdict, steps: set[str], now: datetime,
                  mode: str) -> str | None:
    """Act on a PR held only by CI on an old base: close it when local git
    shows its head already carries the base, else bring it current or ask its
    author to rebase (`backlog_sweep.classify`). A request at this head that
    ended without bringing it current leads to the ask, or, when the worker
    ended it, to one more try. Returns the booked step, or None."""
    req = pr.fix_request or {}
    head = pr.head_sha
    retry = False
    if req.get("against_head_sha") == head and req.get("action") in ("update", "rebase", "resolve"):
        retry = "requeued" not in steps and backlog_sweep.ended_on_the_machine(req, head)
        if not retry:
            conflicts = [str(p) for p in (req.get("result") or {}).get("conflict_paths") or []]
            return _ask(pr, conflicts, mode,
                        f"its {req.get('action')} request ended {req.get('status')}")
    backlog_sweep.fetch([pr.number])
    probe = backlog_sweep.probe(pr.number, head)
    if isinstance(probe, str) or verdict.live is None:
        return None
    if probe.current:
        again = _verdict(pr, verdict.live._replace(has_base=True), now)
        return close(pr, again.feedback, again.why, mode) if again.close else None
    plan = backlog_sweep.classify(pr, probe)
    if plan.action is None:
        return _ask(pr, plan.conflicts, mode, plan.why)
    if mode != "live":
        return _book(pr, "dry-run", mode, f"would queue {plan.action}: {plan.why}")
    try:
        fix_queue.queue_pr(pr.number, plan.action)
    except ValueError as e:
        return _book(pr, "failed", mode, f"not queued: {e}")
    return _book(pr, "requeued" if retry else "queued", mode, plan.why, action=plan.action)


def _ask(pr: Pr, conflicts: list[str], mode: str, why: str) -> str:
    if mode != "live":
        return _book(pr, "dry-run", mode, f"would ask for a rebase: {why}")
    res = backlog_sweep.ask_rebase(pr, conflicts)
    if res.get("status") not in ("executed", "exists"):
        return _book(pr, "failed", mode, why, detail=res.get("detail"))
    return _book(pr, "asked", mode, why, status=res.get("status"))


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
            steps = heads.get((pr.number, str(pr.head_sha)), set())
            if steps & FINAL_STEPS:
                continue
            verdict = judge(pr, now)
            if verdict.close:
                step = close(pr, verdict.feedback, verdict.why, mode)
            elif verdict.update:
                step = bring_current(pr, verdict, steps, now, mode)
            else:
                step = None
            if step is not None:
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
        v = judge(pr, now)
        verb = "close" if v.close else "update" if v.update else "keep"
        rows.append({"pr": pr.number, "author": pr.author, "verdict": verb, "why": v.why,
                     "feedback": v.feedback})
        print(f"#{pr.number:<6} {verb:<6} @{pr.author}  {v.why}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(rows, f, indent=1)
    count = {v: sum(r["verdict"] == v for r in rows) for v in ("close", "update", "keep")}
    print(json.dumps({"candidates": len(rows), **count,
                      "daily_cap": settings.abandoned_close_daily(),
                      "mode": settings.abandoned_close()}))
    return 0
