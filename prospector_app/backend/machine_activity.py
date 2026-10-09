"""What every machine did in the past day — the Control tab's Recent activity.

`summarize` folds plain data: the PR, issue and alert runs ledgers (each row
naming the machine that wrote it, `storekit.stamp_host`), the agent ledger's
spend, the machine roster, and this app's own jobs. A row lands in one bucket:
a lane (security, verify, autofix, issue fix) counted by outcome, a background
pass counted by kind, or a job a person started. `activity` gathers those
inputs from the app's cached reads.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal, TypedDict

from pipeline import capacity
from pipeline import storekit
from pipeline import worker_health

if TYPE_CHECKING:
    from prospector_app.backend.jobs import JobView

Lane = Literal["security", "verify", "autofix", "issue_fix"]

LANE_PHASES: dict[str, Lane] = {
    "security:review-one": "security", "verify:single": "verify",
    "fix:single": "autofix", "issue-fix:run": "issue_fix"}
# Each background phase's name for one pass and for several.
BACKGROUND: dict[str, tuple[str, str]] = {
    "ingest:watch": ("PR watch", "PR watches"), "threat-scan:heads": ("threat scan", "threat scans"),
    "rereview:request": ("re-review request", "re-review requests"),
    "verify:pin-refresh": ("base pin refresh", "base pin refreshes"),
    "reingest": ("re-ingest", "re-ingests"),
    "threat-evidence:capture": ("evidence capture", "evidence captures")}
# Phases a Control-tab job writes and the clustering lane writes too; the
# lane's rows carry `trigger: worker`.
WORKER_BACKGROUND: dict[str, tuple[str, str]] = {
    "cluster:summaries": ("summary batch", "summary batches"),
    "cluster:assign": ("cluster placement", "cluster placements"),
    "analyze:commit": ("cluster analysis", "cluster analyses")}
WORKER_TRIGGERS = frozenset({"worker", "autohunt", "hunter"})
UNATTRIBUTED = "unattributed"
WINDOW_HOURS = 24


class Outcome(TypedDict):
    label: str
    count: int
    numbers: list[int]


class LaneActivity(TypedDict):
    count: int
    numbers: list[int]
    outcomes: list[Outcome]


class Background(TypedDict):
    label: str
    count: int


class JobRun(TypedDict):
    label: str
    kind: str
    status: str
    job_id: int | None


class Current(TypedDict):
    pr: int | None
    issue: int | None


class MachineActivity(TypedDict):
    host: str
    local: bool
    online: bool
    offline_since: str | None
    # Silent past worker_health.OFFLINE_AFTER_SECONDS: down, not restarting.
    stalled: bool
    has_worker: bool
    tripped: list[str]
    current: Current
    lanes: dict[Lane, LaneActivity]
    background: list[Background]
    jobs: list[JobRun]
    spend_usd: float


class ActivityView(TypedDict):
    local: str
    window_hours: int
    machines: list[MachineActivity]


def row_host(raw: dict) -> str | None:
    """The machine a ledger row names, at its top level or in its `stats`."""
    stats = raw.get("stats")
    host = raw.get("host") or (stats.get("host") if isinstance(stats, dict) else None)
    return str(host) if host else None


def _when(raw: dict) -> datetime | None:
    return storekit.parse_ts(raw.get("finished") or raw.get("started"))


def _outcome(lane: Lane, stats: dict) -> str:
    if lane == "security":
        return str(stats.get("verdict") or "no verdict")
    if lane == "verify":
        status = stats.get("status")
        if status == "waiting-for-base":
            return "waiting for base"
        if status == "error":
            return f"error: {stats.get('error_kind') or 'unknown'}"
        return str(stats.get("outcome") or status or "unknown")
    if lane == "autofix":
        status = str(stats.get("status") or "unknown")
        if status == "failed" and stats.get("kind") == capacity.PAUSED_KIND:
            return "waiting for AI capacity"
        return "parked" if status == "awaiting-review" else status
    return str(stats.get("ending") or "unknown")


def _number(lane: Lane, raw: dict) -> int | None:
    n = raw.get("issue" if lane == "issue_fix" else "pr")
    return n if isinstance(n, int) else None


def _blank(host: str, local: str) -> MachineActivity:
    return {"host": host, "local": host == local, "online": False, "offline_since": None,
            "stalled": False, "has_worker": False, "tripped": [], "current": {"pr": None, "issue": None},
            "lanes": {}, "background": [], "jobs": [], "spend_usd": 0.0}


def _from_roster(m: dict, local: str, now: datetime) -> MachineActivity:
    entry = _blank(str(m["host"]), local)
    beats: dict[str, dict] = m.get("beats") or {}
    entry["online"] = bool(m.get("online"))
    entry["has_worker"] = bool(beats)
    stamps = [str(b["last_beat"]) for b in beats.values() if b.get("last_beat")]
    if not entry["online"] and stamps:
        entry["offline_since"] = max(stamps, key=lambda s: storekit.parse_ts(s)
                                     or datetime.min.replace(tzinfo=timezone.utc))
        last = storekit.parse_ts(entry["offline_since"])
        entry["stalled"] = (last is not None and (now - last).total_seconds()
                            >= worker_health.OFFLINE_AFTER_SECONDS)
    entry["tripped"] = sorted(lane for lane, h in (m.get("lanes") or {}).items()
                              if h.get("tripped"))
    for b in beats.values():
        if b.get("online") and (b.get("current_pr") is not None or b.get("current_issue") is not None):
            entry["current"] = {"pr": b.get("current_pr"), "issue": b.get("current_issue")}
            break
    return entry


def _lane(outcomes: dict[str, list[int | None]]) -> LaneActivity:
    rows = [n for ns in outcomes.values() for n in ns]
    ordered = sorted(outcomes.items(), key=lambda kv: -len(kv[1]))
    return {"count": len(rows),
            "numbers": sorted({n for n in rows if n is not None}),
            "outcomes": [{"label": label, "count": len(ns),
                          "numbers": sorted({n for n in ns if n is not None})}
                         for label, ns in ordered]}


def summarize(rows: list[tuple[str, dict]], spend: dict[str, float], roster: dict,
              local_jobs: list[JobView], job_phases: dict[tuple[str, str], tuple[str, str]],
              now: datetime) -> ActivityView:
    """Every machine's past `WINDOW_HOURS`: `rows` are `(ledger, record)`
    pairs from the PR, issue and alert ledgers, `job_phases` maps a Control-tab
    job's `(ledger, phase)` to its `(kind, label)`, and `local_jobs` are this
    app's job records, which stand in for the ledger's jobs on the machine
    serving it. `spend` is each host's reported agent cost over the window,
    summed by the store (`Store.agent_spend`)."""
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    local = str(roster.get("local") or "")
    machines: dict[str, MachineActivity] = {
        str(m["host"]): _from_roster(m, local, now) for m in roster.get("machines") or []}
    lanes: dict[str, dict[Lane, dict[str, list[int | None]]]] = {}
    background: dict[str, Counter[tuple[str, str]]] = {}
    jobs: dict[str, dict[str, JobRun]] = {}

    for ledger, raw in rows:
        when = _when(raw)
        if when is None or when < cutoff or when > now:
            continue
        phase = raw.get("phase")
        if not isinstance(phase, str):
            continue
        host = row_host(raw) or UNATTRIBUTED
        trigger = raw.get("trigger")
        if phase in LANE_PHASES:
            lane = LANE_PHASES[phase]
            stats = s if isinstance(s := raw.get("stats"), dict) else {}
            (lanes.setdefault(host, {}).setdefault(lane, {})
             .setdefault(_outcome(lane, stats), []).append(_number(lane, raw)))
        elif phase in BACKGROUND:
            background.setdefault(host, Counter())[BACKGROUND[phase]] += 1
        elif phase in WORKER_BACKGROUND and trigger in WORKER_TRIGGERS:
            background.setdefault(host, Counter())[WORKER_BACKGROUND[phase]] += 1
        elif (ledger, phase) in job_phases and trigger not in WORKER_TRIGGERS:
            kind, label = job_phases[(ledger, phase)]
            jobs.setdefault(host, {}).setdefault(
                label, {"label": label, "kind": kind, "status": "done", "job_id": None})
        else:
            continue
        machines.setdefault(host, _blank(host, local))

    for host, by_lane in lanes.items():
        machines[host]["lanes"] = {lane: _lane(outcomes) for lane, outcomes in by_lane.items()}
    for host, counts in background.items():
        machines[host]["background"] = [{"label": one if n == 1 else many, "count": n}
                                        for (one, many), n in counts.most_common()]
    for host, by_label in jobs.items():
        machines[host]["jobs"] = list(by_label.values())

    mine = [job for job in local_jobs
            if (ts := storekit.parse_ts(job["started"])) is not None and cutoff <= ts <= now]
    if mine:
        machines.setdefault(local, _blank(local, local))
    if local in machines:
        machines[local]["jobs"] = [
            {"label": j["label"], "kind": j["kind"], "status": j["status"], "job_id": j["id"]}
            for j in sorted(mine, key=lambda j: j["id"])] if mine else []

    for host, usd in spend.items():
        if host in machines:
            machines[host]["spend_usd"] = round(usd, 2)

    ordered = sorted(machines.values(),
                     key=lambda m: (m["host"] == UNATTRIBUTED, not m["online"], m["host"]))
    return {"local": local, "window_hours": WINDOW_HOURS, "machines": ordered}


def _job_phases() -> dict[tuple[str, str], tuple[str, str]]:
    from prospector_app.backend import jobs
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for kind, spec in jobs.JOB_SPECS.items():
        ledger = spec.get("ledger")
        if ledger is None:
            continue
        name, phases = ledger
        for phase in phases:
            if phase not in LANE_PHASES:
                out.setdefault((name, phase), (kind, spec["label"]))
    return out


def activity() -> ActivityView:
    from prospector_app.backend import alert_data, data, issues, jobs, machines
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=WINDOW_HOURS)).isoformat()
    rows: list[tuple[str, dict]] = []
    for ledger, records in (("pr", data.runs(since=since)), ("issue", issues.cached_runs()),
                            ("alert", alert_data.runs())):
        rows.extend((ledger, r.raw) for r in records if isinstance(r, storekit.PhaseRun))
    spend = data.store().agent_spend(since, "host", until=now.isoformat())
    return summarize(rows, {h: usd for h, usd in spend.items() if h is not None},
                     machines.roster(), jobs.list_jobs(), _job_phases(), now)
