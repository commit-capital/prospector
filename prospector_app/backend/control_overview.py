"""The Health & queues overview: one read of which machines and lanes are on,
what runs now, what runs next, what ran, and what needs a person.

`build` is pure over plain data; `gather` reads the live inputs. A lane's
state on a machine is the first that holds of: stood down, offline, off (its
flag), tripped, AI paused, on.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Literal, TypedDict

from pipeline import capacity, gates, storekit
from prospector_app.backend import machine_activity, worker_loops

LANES: tuple[str, ...] = ("security", "verify", "design", "autofix", "issue-fix", "cluster")
LABELS: dict[str, str] = {"security": "Security", "verify": "Verify", "design": "Design",
                          "autofix": "Autofix", "issue-fix": "Issue fix", "cluster": "Clustering"}
# worker_health's name for each lane.
HEALTH_LANE: dict[str, str] = {"security": "security", "verify": "verify", "design": "design",
                               "autofix": "fix", "issue-fix": "issue-fix", "cluster": "cluster"}
# The heartbeat each lane runs under, for a machine with no worker_loops row.
BEAT_LANE: dict[str, str] = {"security": "verify", "verify": "verify", "design": "verify",
                             "autofix": "fix", "issue-fix": "issue-fix", "cluster": "verify"}
# The ledger phase each lane books an ending under.
LANE_PHASES: dict[str, str] = {"security:review-one": "security", "verify:single": "verify",
                               "design-review:run": "design", "fix:single": "autofix",
                               "issue-fix:run": "issue-fix", "cluster:summaries": "cluster",
                               "cluster:assign": "cluster", "analyze:commit": "cluster"}
PAST_LIMIT = 60
WINDOW = timedelta(hours=24)

CellState = Literal["on", "off", "tripped", "ai-paused", "stood-down", "offline"]
MachineState = Literal["working", "idle", "off", "offline"]
NeedKind = Literal["trip", "offline", "pins", "failed-runs", "parked"]


class Cell(TypedDict):
    host: str
    state: CellState


class NowItem(TypedDict):
    host: str | None
    pr: int | None
    issue: int | None
    title: str | None
    step: str | None
    started_at: str | None


class QueueItem(TypedDict):
    pr: int | None
    issue: int | None
    title: str | None
    status: str
    source: str | None
    queued_at: str | None


class LaneRow(TypedDict):
    lane: str
    label: str
    cells: list[Cell]
    now: list[NowItem]
    queue: list[QueueItem]
    parked: int
    pool: int | None
    ended_24h: int
    faults_24h: int


class LoopRow(TypedDict):
    loop: str
    label: str
    host: str
    last_at: str | None
    next_at: str | None


class Machine(TypedDict):
    host: str
    local: bool
    state: MachineState
    last_beat: str | None
    lanes_on: int
    stood_down: bool
    stood_down_note: str | None


class Need(TypedDict):
    key: str
    kind: NeedKind
    title: str
    at: str | None
    host: str | None
    lane: str | None
    detail: str | None
    count: int
    groups: list[dict]


class TimelineItem(TypedDict):
    when: Literal["past", "now", "upcoming"]
    at: str
    lane: str
    host: str | None
    pr: int | None
    issue: int | None
    text: str
    bad: bool


class AiPaused(TypedDict):
    reason: str
    retry_at: str | None


class Overview(TypedDict):
    local: str
    machines: list[Machine]
    lanes: list[LaneRow]
    loops: list[LoopRow]
    needs: list[Need]
    timeline: list[TimelineItem]
    ai_paused: AiPaused | None


class Inputs(TypedDict):
    local: str
    roster: dict                              # machines.roster()
    stood_down: dict[str, dict]               # machine_mode.stood_down(...)
    loops: dict[str, dict]                    # Store.load_worker_loops()
    tripped: dict[str, dict[str, dict]]       # host → health lane → trip stamp {at, kind, reason}
    pins: list[dict]                          # autohunt_view.base_health()["hosts"] in trouble
    ai_paused: dict[str, AiPaused]            # account key → decision, paused accounts only
    verify_queue: list[dict]                  # verify_queue.queue_entries()
    fix_queue: list[dict]                     # fix_queue.queue_entries()
    issue_queue: list[dict]                   # {issue, title, status, source, queued_at, started_at, host, step}
    hunter_now: dict[str, list[NowItem]]      # "security"/"design" → in-flight hunter runs
    pools: dict[str, int]                     # security / verify / design pool sizes
    failed_runs: list[dict]                   # [{lane, reason, prs}]
    rows: list[tuple[str, dict]]              # (ledger, record) in the past 24h


def _beat_lanes(m: dict) -> dict[str, bool]:
    beats = m.get("beats") or {}
    return {lane: BEAT_LANE[lane] in beats and lane != "cluster" for lane in LANES}


def _cell(lane: str, host: str, m: dict, inp: Inputs) -> CellState:
    if host in inp["stood_down"]:
        return "stood-down"
    if not m.get("online"):
        return "offline"
    row = inp["loops"].get(host)
    on = (row or {}).get("lanes") if row else _beat_lanes(m)
    if not (on or {}).get(lane):
        return "off"
    if HEALTH_LANE[lane] in inp["tripped"].get(host, {}):
        return "tripped"
    acct = (m.get("ai_account") or {}).get("key")
    if acct and acct in inp["ai_paused"]:
        return "ai-paused"
    return "on"


def _outcome_bad(lane: str, stats: dict) -> bool:
    if lane == "verify":
        return stats.get("status") == "error" and stats.get("error_kind") in gates.VERIFY_REQUEST_FAULT
    if lane == "autofix":
        return stats.get("status") == "failed" and stats.get("kind") != capacity.PAUSED_KIND
    if lane == "issue-fix":
        return stats.get("ending") == "failed"
    return stats.get("status") == "failed"


def _outcome_text(lane: str, stats: dict) -> str:
    if lane == "security" or lane == "verify" or lane == "autofix":
        return machine_activity.outcome(lane, stats)
    return str(stats.get("ending") or stats.get("status") or stats.get("outcome") or "done")


def _need_rows(inp: Inputs, machines: list[Machine], fq: list[dict]) -> list[Need]:
    def need(key: str, kind: NeedKind, title: str, *, at: str | None = None,
             host: str | None = None, lane: str | None = None, detail: str | None = None,
             count: int = 1, groups: list[dict] | None = None) -> Need:
        return {"key": key, "kind": kind, "title": title, "at": at, "host": host,
                "lane": lane, "detail": detail, "count": count, "groups": groups or []}

    out: list[Need] = []
    online = {m["host"] for m in machines if m["state"] in ("working", "idle")}
    for host, lanes in sorted(inp["tripped"].items()):
        if host not in online:
            continue
        for lane, t in sorted(lanes.items()):
            label = next((LABELS[k] for k, v in HEALTH_LANE.items() if v == lane), lane)
            out.append(need(f"trip:{host}:{lane}", "trip",
                            f"{label} lane tripped on {host}: {t.get('kind') or 'unknown'}",
                            at=t.get("at"), host=host, lane=lane, detail=t.get("reason")))
    for m in machines:
        if m["state"] == "offline":
            out.append(need(f"offline:{m['host']}", "offline", f"{m['host']} worker offline",
                            at=m["last_beat"], host=m["host"]))
    pins = [p for p in inp["pins"] if p.get("host") not in inp["stood_down"]]
    if pins:
        out.append(need("pins", "pins", f"Base pin refresh failing on {len(pins)} "
                        f"machine{'s' if len(pins) != 1 else ''}", count=len(pins),
                        detail="\n".join(f"{p['host']}: {p.get('refresh_error') or 'stale'}"
                                         for p in pins)))
    runs = sum(len(g["prs"]) for g in inp["failed_runs"])
    if runs:
        out.append(need("failed-runs", "failed-runs", f"{runs} failed hunter "
                        f"run{'s' if runs != 1 else ''}", count=runs, groups=inp["failed_runs"]))
    parked = [e for e in fq if e.get("status") == "awaiting-review"]
    if parked:
        oldest = min((e.get("queued_at") or "" for e in parked), default="") or None
        out.append(need("parked", "parked", f"{len(parked)} parked change"
                        f"{'s' if len(parked) != 1 else ''} waiting for your push",
                        at=oldest, count=len(parked), lane="autofix"))
    return out


def build(inp: Inputs, now: datetime) -> Overview:
    cutoff = now - WINDOW
    roster = inp["roster"].get("machines") or []
    machines: list[Machine] = []
    lane_cells: dict[str, list[Cell]] = {lane: [] for lane in LANES}
    for m in roster:
        host = str(m["host"])
        cells: dict[str, CellState] = {lane: _cell(lane, host, m, inp) for lane in LANES}
        for lane, cell_state in cells.items():
            lane_cells[lane].append({"host": host, "state": cell_state})
        beats = [b.get("last_beat") for b in (m.get("beats") or {}).values() if b.get("last_beat")]
        busy = any(b.get("current_pr") is not None or b.get("current_issue") is not None
                   for b in (m.get("beats") or {}).values())
        n_on = sum(1 for s in cells.values() if s in ("on", "tripped", "ai-paused"))
        state: MachineState = ("off" if host in inp["stood_down"] or (m.get("online") and n_on == 0)
                               else "offline" if not m.get("online")
                               else "working" if busy else "idle")
        machines.append({"host": host, "local": host == inp["local"], "state": state,
                         "last_beat": max(beats, default=None), "lanes_on": n_on,
                         "stood_down": host in inp["stood_down"],
                         "stood_down_note": (inp["stood_down"].get(host) or {}).get("note")})

    vq, fq, iq = inp["verify_queue"], inp["fix_queue"], inp["issue_queue"]

    def now_of(entries: list[dict], running: tuple[str, ...], key: str) -> list[NowItem]:
        return [{"host": e.get("host"), "pr": e.get("pr") if key == "pr" else None,
                 "issue": e.get("issue") if key == "issue" else None, "title": e.get("title"),
                 "step": e.get("step"), "started_at": e.get("started_at")}
                for e in entries if e.get("status") in running]

    def queue_of(entries: list[dict], waiting: tuple[str, ...], key: str) -> list[QueueItem]:
        return [{"pr": e.get("pr") if key == "pr" else None,
                 "issue": e.get("issue") if key == "issue" else None, "title": e.get("title"),
                 "status": str(e.get("status")), "source": e.get("source"),
                 "queued_at": e.get("queued_at")}
                for e in entries if e.get("status") in waiting]

    nows = {"verify": now_of(vq, ("running",), "pr"),
            "autofix": now_of(fq, ("running", "pushing"), "pr"),
            "issue-fix": now_of(iq, ("running",), "issue"),
            "security": inp["hunter_now"].get("security", []),
            "design": inp["hunter_now"].get("design", []), "cluster": []}
    queues = {"verify": queue_of(vq, ("queued", "waiting-for-base"), "pr"),
              "autofix": queue_of(fq, ("queued", "approved"), "pr"),
              "issue-fix": queue_of(iq, ("queued",), "issue")}

    ended: Counter[str] = Counter()
    faults: Counter[str] = Counter()
    past: list[TimelineItem] = []
    background: Counter[tuple[str, str, str | None]] = Counter()
    for _ledger, raw in inp["rows"]:
        when = storekit.parse_ts(raw.get("finished") or raw.get("started"))
        phase = raw.get("phase")
        if when is None or when < cutoff or when > now or not isinstance(phase, str):
            continue
        host = machine_activity.row_host(raw)
        stats = s if isinstance(s := raw.get("stats"), dict) else {}
        if phase in LANE_PHASES:
            lane = LANE_PHASES[phase]
            bad = _outcome_bad(lane, stats)
            ended[lane] += 1
            faults[lane] += bad
            pr = raw.get("pr") if isinstance(raw.get("pr"), int) else None
            issue = raw.get("issue") if isinstance(raw.get("issue"), int) else None
            text = _outcome_text(lane, stats)
            past.append({"when": "past", "at": when.isoformat(), "lane": lane, "host": host,
                         "pr": pr, "issue": issue, "bad": bad,
                         "text": f"#{pr or issue} → {text}" if (pr or issue) else text})
        elif phase in machine_activity.BACKGROUND:
            hour = when.replace(minute=0, second=0, microsecond=0).isoformat()
            background[(machine_activity.BACKGROUND[phase][0], hour, host)] += 1
    for (label, hour, host), n in background.items():
        past.append({"when": "past", "at": hour, "lane": "background", "host": host,
                     "pr": None, "issue": None, "bad": False,
                     "text": label if n == 1 else f"{label} ×{n}"})
    past.sort(key=lambda t: t["at"], reverse=True)

    online = {m["host"] for m in machines if m["state"] in ("working", "idle")}
    loops: list[LoopRow] = []
    for host, row in sorted(inp["loops"].items()):
        for name, p in sorted((row.get("loops") or {}).items()):
            name = str(name)
            last = storekit.parse_ts(p.get("last_at"))
            nxt = (last + timedelta(seconds=int(p.get("every_seconds") or 0))
                   if last and host in online else None)
            loops.append({"loop": name, "label": worker_loops.LABELS.get(name, name),
                          "host": host, "last_at": p.get("last_at"),
                          "next_at": nxt.isoformat() if nxt else None})

    upcoming: list[TimelineItem] = [
        {"when": "upcoming", "at": lp["next_at"], "lane": "background", "host": lp["host"],
         "pr": None, "issue": None, "bad": False, "text": lp["label"]}
        for lp in loops if lp["next_at"] and lp["next_at"] <= (now + timedelta(hours=1)).isoformat()]
    for lane, q in queues.items():
        if q:
            upcoming.append({"when": "upcoming", "at": now.isoformat(), "lane": lane, "host": None,
                             "pr": q[0]["pr"], "issue": q[0]["issue"], "bad": False,
                             "text": f"next: #{q[0]['pr'] or q[0]['issue']}"})
    upcoming.sort(key=lambda t: t["at"])
    current: list[TimelineItem] = [
        {"when": "now", "at": n["started_at"] or now.isoformat(), "lane": lane, "host": n["host"],
         "pr": n["pr"], "issue": n["issue"], "bad": False,
         "text": f"#{n['pr'] or n['issue']}" + (f" · {n['step']}" if n["step"] else "")}
        for lane, items in nows.items() for n in items]

    lanes: list[LaneRow] = [{
        "lane": lane, "label": LABELS[lane], "cells": lane_cells[lane], "now": nows[lane],
        "queue": queues.get(lane, []),
        "parked": sum(1 for e in fq if e.get("status") == "awaiting-review") if lane == "autofix" else 0,
        "pool": inp["pools"].get(lane), "ended_24h": ended[lane], "faults_24h": faults[lane],
    } for lane in LANES]

    paused = next(iter(inp["ai_paused"].values()), None)
    return {"local": inp["local"], "machines": machines, "lanes": lanes, "loops": loops,
            "needs": _need_rows(inp, machines, fq),
            "timeline": upcoming + current + past[:PAST_LIMIT], "ai_paused": paused}


def _issue_queue() -> list[dict]:
    from issue_triage import fix_review
    from issue_triage.issue_store import IssueStore
    issues = IssueStore().issues_matching(("fix_request", "status"), ["queued", "running"])
    order = {n: i for i, n in enumerate(fix_review.queued_issues(issues))}
    out: list[dict] = []
    for n, issue in issues.items():
        req = issue.fix_request or {}
        out.append({"issue": n, "title": issue.title, "status": req.get("status"),
                    "source": req.get("source"), "queued_at": req.get("queued_at"),
                    "started_at": req.get("started_at"), "host": req.get("host"),
                    "step": req.get("step")})
    return sorted(out, key=lambda e: (e["status"] != "running", order.get(e["issue"], 1 << 30)))


def _hunter_now(roster: dict, verify_running: set[int]) -> dict[str, list[NowItem]]:
    """The security and design reviews in flight: a verify worker's current PR
    that no verify request accounts for, laned by which claim its host holds."""
    from prospector_app.backend import data
    prs = data.prs()
    out: dict[str, list[NowItem]] = {"security": [], "design": []}
    for m in roster.get("machines") or []:
        beat = (m.get("beats") or {}).get("verify") or {}
        n = beat.get("current_pr")
        if not beat.get("online") or n is None or n in verify_running:
            continue
        pr = prs.get(n)
        design = (pr.raw.get("design_run") or {}) if pr else {}
        lane = "design" if design.get("host") == m["host"] else "security"
        claim = design if lane == "design" else ((pr.raw.get("security_run") or {}) if pr else {})
        out[lane].append({"host": m["host"], "pr": n, "issue": None,
                          "title": pr.title if pr else None, "step": None,
                          "started_at": claim.get("started_at")})
    return out


def gather() -> Overview:
    from pipeline import design_review, settings
    from prospector_app.backend import (alert_data, autohunt_view, capacity_view, data, escalation,
                                        fix_queue, issues, machine_mode, machines, verify_queue)
    now = datetime.now(timezone.utc)
    st = data.store()
    roster = machines.roster()
    tripped = {str(h["host"]): {lane: ((h.get("lanes") or {}).get(lane) or {}).get("tripped") or {}
                                for lane in h["tripped"]}
               for h in escalation.health_status()["hosts"] if h.get("tripped")}
    hunt = autohunt_view.status()
    pins = [dict(p) for p in hunt["base"]["hosts"] if p["refresh_failures"] > 0 or p["stale"]]
    paused: dict[str, AiPaused] = {
        a["key"]: {"reason": a["decision"]["reason"], "retry_at": a["decision"]["retry_at"]}
        for a in capacity_view.accounts() if a["machines"] and not a["decision"]["allowed"]}
    vq = [dict(e) for e in verify_queue.queue_entries()]
    groups: dict[tuple[str, str], list[int]] = {}
    for n in hunt["security_failed"]:
        reason = (hunt["security_failed_reasons"].get(str(n)) or "run failed").strip()
        groups.setdefault(("security", reason), []).append(n)
    for f in hunt["verify_failed"]:
        groups.setdefault(("verify", (f.get("error_kind") or "run failed").strip()), []).append(f["pr"])
    since = (now - WINDOW).isoformat()
    rows: list[tuple[str, dict]] = []
    for ledger, records in (("pr", data.runs(since=since)), ("issue", issues.cached_runs()),
                            ("alert", alert_data.runs())):
        rows.extend((ledger, r.raw) for r in records if isinstance(r, storekit.PhaseRun))
    open_prs = [pr for pr in data.prs().values() if pr.state == "open"]
    inp: Inputs = {
        "local": settings.worker_id(), "roster": roster,
        "stood_down": machine_mode.stood_down(st.load_machine_modes(), roster),
        "loops": st.load_worker_loops(), "tripped": tripped, "pins": pins, "ai_paused": paused,
        "verify_queue": vq, "fix_queue": [dict(e) for e in fix_queue.queue_entries()],
        "issue_queue": _issue_queue(),
        "hunter_now": _hunter_now(roster, {int(str(e["pr"])) for e in vq if e["status"] == "running"}),
        "pools": {"security": hunt["security_pool"], "verify": hunt["verify_pool"],
                  "design": sum(1 for pr in open_prs if design_review.huntable(pr))},
        "failed_runs": [{"lane": lane, "reason": reason, "prs": prs}
                        for (lane, reason), prs in sorted(groups.items(), key=lambda kv: -len(kv[1]))],
        "rows": rows}
    return build(inp, now)
