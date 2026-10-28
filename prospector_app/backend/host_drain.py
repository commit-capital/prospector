"""Let this machine's worker lanes finish what they carry out, so its backend
can stop without cutting a run short (`update-prospector.sh`).

`pause` pauses every lane on this host (lane_pause) as DRAIN_BY, leaving a
lane an operator paused as it is; `resume` lifts only DRAIN_BY's pauses.
`runs` names each run in flight here from the lanes' heartbeats, with how long
that kind of run takes on this host (the median of its finished runs). The
drain is done once every lane beating here has beaten since the pause with
nothing in flight.
"""
from __future__ import annotations

import argparse
import statistics
import time
from datetime import datetime, timezone
from typing import TypedDict

from issue_triage.issue_store import IssueStore
from pipeline import settings, storekit, worker_health
from prospector_app.backend import data, lane_pause

DRAIN_BY = "update-prospector"
# A heartbeat older than this is a lane that is not running.
ONLINE_SECONDS = 120.0
POLL_SECONDS = 10.0
# Request statuses that have not ended, per lane.
_ISSUE_ACTIVE = ("queued", "running")
_VERIFY_ACTIVE = ("queued", "running", "waiting-for-base")
_FIX_ACTIVE = ("queued", "running", "awaiting-review", "approved", "pushing")


class Run(TypedDict):
    lane: str
    item: str
    action: str | None
    title: str | None
    elapsed_s: float | None
    typical_s: float | None


def pause() -> list[str]:
    """Pause every open lane on this host; the lanes it paused."""
    host = settings.worker_id()
    held = data.store().load_lane_pauses()
    lanes = [lane for lane in worker_health.LANES if not lane_pause.paused(host, lane, held)]
    for lane in lanes:
        lane_pause.pause(host, lane, by=DRAIN_BY)
    return lanes


def resume() -> list[str]:
    """Lift the drain's pauses on this host; the lanes it reopened."""
    host = settings.worker_id()
    rec = (data.store().load_lane_pauses().get(host) or {}).get("lanes") or {}
    lanes = [lane for lane, p in rec.items() if (p or {}).get("by") == DRAIN_BY]
    for lane in lanes:
        lane_pause.resume(host, lane)
    return lanes


def _seconds(start: str | None, end: str | None) -> float | None:
    a, b = storekit.parse_ts(start), storekit.parse_ts(end)
    return (b - a).total_seconds() if a and b else None


def _typical(reqs: list[dict], active: tuple[str, ...]) -> dict[str | None, float]:
    """Per action, the median length of the finished requests in `reqs`."""
    by: dict[str | None, list[float]] = {}
    for r in reqs:
        if r.get("status") not in active and (s := _seconds(r.get("started_at"), r.get("finished_at"))):
            by.setdefault(r.get("action"), []).append(s)
    return {k: statistics.median(v) for k, v in by.items()}


def _beats(now: datetime) -> dict[str, dict]:
    """This host's online heartbeat per worker: verify, fix, issue-fix."""
    regs = data.store().load_worker_registries()
    host = settings.worker_id()
    out: dict[str, dict] = {}
    for lane, reg in (("verify", regs["verify_worker"]), ("fix", regs["fix_worker"]),
                      ("issue-fix", regs["issue_fix_worker"])):
        rec = (reg.get("hosts") or {}).get(host) or {}
        beat = storekit.parse_ts(rec.get("last_beat"))
        if beat and (now - beat).total_seconds() < ONLINE_SECONDS:
            out[lane] = rec
    return out


def _run(lane: str, item: str, req: dict, title: str | None, typical: dict[str | None, float],
         now: datetime) -> Run:
    start = storekit.parse_ts(req.get("started_at"))
    return {"lane": lane, "item": item, "action": req.get("action"), "title": title,
            "elapsed_s": (now - start).total_seconds() if start else None,
            "typical_s": typical.get(req.get("action"))}


def runs(beats: dict[str, dict] | None = None) -> list[Run]:
    """The runs in flight on this host, longest still to go first."""
    now = datetime.now(timezone.utc)
    beats = _beats(now) if beats is None else beats
    host = settings.worker_id()
    out: list[Run] = []
    st = data.store()
    if issues := beats.get("issue-fix", {}).get("current_issues"):
        mine = IssueStore().issues_matching(("fix_request", "host"), [host])
        typical = _typical([i.fix_request or {} for i in mine.values()], _ISSUE_ACTIVE)
        for n in issues:
            i = mine.get(n)
            out.append(_run("issue-fix", f"issue #{n}", (i.fix_request or {}) if i else {},
                            i.title if i else None, typical, now))
    for lane, path, active in (("verify", "verify_request", _VERIFY_ACTIVE),
                               ("fix", "fix_request", _FIX_ACTIVE)):
        n = beats.get(lane, {}).get("current_pr")
        if n is None:
            continue
        mine = st.prs_matching((path, "host"), [host])
        reqs = {k: getattr(p, path) or {} for k, p in mine.items()}
        req = reqs.get(n) or {}
        # The verify worker also runs security and design reviews, which hold no request.
        running = req.get("status") == "running" or lane == "fix"
        out.append(_run(lane if running else "review", f"PR #{n}", req if running else {},
                        mine[n].title if n in mine else None,
                        _typical(list(reqs.values()), active) if running else {}, now))
    return sorted(out, key=lambda r: -_left(r))


def _left(r: Run) -> float:
    if r["typical_s"] is None or r["elapsed_s"] is None:
        return 0.0
    return max(0.0, r["typical_s"] - r["elapsed_s"])


def drained() -> bool:
    """Whether every lane beating here has beaten since the drain paused it,
    with nothing in flight."""
    now = datetime.now(timezone.utc)
    rec = (data.store().load_lane_pauses().get(settings.worker_id()) or {}).get("lanes") or {}
    stamps = [t for p in rec.values() if (p or {}).get("by") == DRAIN_BY
              and (t := storekit.parse_ts(p.get("at")))]
    since = max(stamps) if stamps else now
    beats = _beats(now)
    fresh = all((storekit.parse_ts(b.get("last_beat")) or since) > since for b in beats.values())
    return fresh and not runs(beats)


def _minutes(s: float) -> str:
    m = round(s / 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{max(m, 1)}m"


def describe(r: Run) -> str:
    what = " ".join(x for x in (r["item"], r["action"]) if x)
    title = f'  "{r["title"][:50]}"' if r["title"] else ""
    ran = f"running {_minutes(r['elapsed_s'])}" if r["elapsed_s"] is not None else "running"
    if r["typical_s"] is None:
        eta = "no finished run here to go by"
    elif (left := _left(r)) > 0:
        eta = f"usually {_minutes(r['typical_s'])} → ~{_minutes(left)} left"
    else:
        eta = f"past its usual {_minutes(r['typical_s'])}"
    return f"  {r['lane']:<9} {what}{title}\n            {ran} · {eta}"


def _wait() -> int:
    started = time.monotonic()
    last = ""
    while not drained():
        now = runs()
        line = (f"waiting {_minutes(time.monotonic() - started)}: "
                + (", ".join(r["item"] for r in now) or "for the lanes' next heartbeat"))
        if line != last:
            print(line, flush=True)
            last = line
        time.sleep(POLL_SECONDS)
    print("every lane is idle", flush=True)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="prospector drain",
        description="Let this machine's worker lanes finish their runs before a restart.")
    parser.add_argument("action", choices=("status", "pause", "wait", "resume"),
                        help="status: list the runs in flight (exit 3 when there are any); "
                             "pause: take no new work; wait: until the paused lanes are idle; "
                             "resume: lift the drain's pauses")
    args = parser.parse_args(argv)
    if args.action == "status":
        now = runs()
        if not now:
            print(f"nothing running on {settings.worker_id()}")
            return 0
        print(f"running on {settings.worker_id()}:")
        for r in now:
            print(describe(r))
        longest = max(_left(r) for r in now)
        if longest:
            print(f"all done in about {_minutes(longest)}")
        return 3
    if args.action == "pause":
        print("paused: " + (", ".join(pause()) or "nothing (every lane was already paused)"))
        return 0
    if args.action == "resume":
        print("resumed: " + (", ".join(resume()) or "nothing"))
        return 0
    try:
        return _wait()
    except KeyboardInterrupt:
        return 130

