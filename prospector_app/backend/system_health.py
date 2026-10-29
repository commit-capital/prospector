"""One systemwide health verdict, for the strip every page shows.

Composes what the shared store already knows — every worker's tripped lanes,
every worker's heartbeat (with the schema version its code writes and its
profile's stamp) and Docker
disk reading, and each ingest's last run — into a short list of
problem items with one overall severity, so an outage on any machine reaches
the operator on whatever page they have open. `summarize` is pure over plain
data; `status` gathers the live inputs.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import NotRequired, TypedDict

from pipeline import settings, storekit, verify_driver, worker_health
from prospector_app.backend import (data, escalation, fix_queue, profile_gate, schema_gate,
                                    threat_view, verify_queue)


class HealthItem(TypedDict):
    kind: str
    severity: str
    label: str
    detail: str | None
    host: str | None


class SystemHealth(TypedDict):
    severity: str
    items: list[HealthItem]
    lanes_total: int
    lanes_down: int
    workers_stalled: bool
    # The open PRs flagged malicious and the credentials to rotate, for the
    # threat banner every page shows beside the strip.
    threats: NotRequired[threat_view.ThreatSummary]


# How stale an ingest is before the strip colors it.
INGEST_AMBER_HOURS = 24.0
INGEST_RED_HOURS = 72.0

# The ingests the strip watches: key in `ingest_last` → human label.
INGEST_LABELS: dict[str, str] = {
    "pr": "PR ingest",
    "issues": "issue ingest",
    "alerts": "alert ingest",
}

_SEVERITY_RANK = {"ok": 0, "amber": 1, "red": 2}

# How long the PR store's latest-ingest stamp is held: the strip polls from
# every open page, and the runs ledger is a whole-table read.
_PR_INGEST_TTL_SECONDS = 60.0
_pr_ingest_cache: tuple[float, str | None] | None = None


def _hours_since(iso: str | None, now_ts: float) -> float | None:
    return storekit.hours_since(iso, datetime.fromtimestamp(now_ts, timezone.utc))


def _age_label(hours: float) -> str:
    if hours < 1:
        return "under 1h"
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours // 24)}d"


def _ingest_item(label: str, last: str | None, now_ts: float) -> HealthItem | None:
    """The strip's item for one ingest, or None while it is fresh. An ingest
    that has never run reads as nothing to report: the strip flags a
    deployment that stopped ingesting, and the Control tab's suggestions own
    first-run nudges."""
    hours = _hours_since(last, now_ts)
    if hours is None or hours < INGEST_AMBER_HOURS:
        return None
    severity = "red" if hours >= INGEST_RED_HOURS else "amber"
    return {"kind": "ingest", "severity": severity, "host": None,
            "label": f"{label} {_age_label(hours)} old",
            "detail": f"last ran {last}"}


def _gb(n: int) -> str:
    return f"{n / 10**9:.0f} GB"


def _disk_item(host: str, disk: dict | None, now_ts: float) -> HealthItem | None:
    """The strip's item for a worker whose Docker disk is below the free space
    its own setting asks for, or full; None for a reading that is healthy,
    unreadable for another reason, or older than the offline cut-off."""
    if not disk:
        return None
    hours = _hours_since(disk.get("at"), now_ts)
    if hours is None or hours * 3600 >= worker_health.OFFLINE_AFTER_SECONDS:
        return None
    remedy = ("Reclaim space on that machine: `uv run python pipeline/verify_driver.py gc` "
              "sweeps base images outside the retention rule and failed builds' leftovers; "
              "`docker system df` shows what else holds the space.")
    error = disk.get("error")
    if error:
        if verify_driver.DISK_FULL not in str(error).lower():
            return None
        return {"kind": "disk", "severity": "red", "host": host,
                "label": f"{host}: Docker disk full", "detail": f"{error}\n{remedy}"}
    free, total = int(disk.get("free_bytes") or 0), int(disk.get("total_bytes") or 0)
    floor = int(disk.get("min_free_bytes") or 0)
    if free >= floor:
        return None
    return {"kind": "disk", "severity": "amber", "host": host,
            "label": f"{host}: Docker disk {_gb(free)} free of {_gb(total)}",
            "detail": f"Below the {_gb(floor)} this worker warns at; a full disk fails "
                      f"every sandbox run.\n{remedy}"}


def _profile_line(host: str, st: dict) -> str:
    tiers = st.get("tier_globs") or []
    globs = " · ".join(f"tier{t} {n}" for t, n in zip((0, 1, 3), tiers))
    gates = ", ".join(st.get("fixable_gates") or []) or "none"
    return f"{host}: {st.get('fingerprint')} · fixable gates {gates} · {globs} globs"


def summarize(health_hosts: list[dict], offline: list[dict],
              worker_lanes: list[tuple[str, str]],
              ingest_last: dict[str, str | None],
              now_ts: float | None = None,
              stood_down: frozenset[str] = frozenset(),
              outdated: dict[str, int] | None = None, store_schema: int = 0,
              me: str | None = None,
              profile_drift: dict[str, dict] | None = None) -> SystemHealth:
    """The strip's answer, from plain data: `health_hosts` is
    `escalation.health_status()["hosts"]`, `offline` is
    `escalation.offline_workers()`, `worker_lanes` every `(lane, host)` the
    worker registries name, and `ingest_last` each ingest's last-run stamp
    keyed per INGEST_LABELS. `stood_down` names the machines an operator
    stood down; they contribute nothing. `outdated` maps each machine whose
    code is behind the store's `store_schema` to its code's version; its lanes
    are down and it reads as needing an update, `me` (this machine) in the
    operator's own words. `profile_drift` maps each live machine to its
    profile stamp when their profiles differ; every machine's lanes are then
    down."""
    now_ts = now_ts if now_ts is not None else time.time()
    health_hosts = [h for h in health_hosts if str(h["host"]) not in stood_down]
    offline = [o for o in offline if str(o["host"]) not in stood_down]
    worker_lanes = [(lane, host) for lane, host in worker_lanes if host not in stood_down]
    tripped_by_host: dict[str, list[str]] = {
        str(h["host"]): list(h["tripped"]) for h in health_hosts if h.get("tripped")}
    offline_by_host: dict[str, float] = {}
    for o in offline:
        host = str(o["host"])
        offline_by_host[host] = max(float(o["age_seconds"]),
                                    offline_by_host.get(host, 0.0))

    # A tripped lane on a host the registries no longer name still counts.
    lanes = set(worker_lanes)
    for host, names in tripped_by_host.items():
        lanes.update((name, host) for name in names)
    outdated = {h: v for h, v in (outdated or {}).items() if h not in stood_down}
    drift = {h: st for h, st in (profile_drift or {}).items() if h not in stood_down}
    drift = drift if len(drift) > 1 else {}
    down = {(lane, host) for lane, host in lanes
            if host in offline_by_host or host in outdated or host in drift
            or lane in tripped_by_host.get(host, [])}

    items: list[HealthItem] = []
    if drift:
        newest = max(int(st.get("version") or 1) for st in drift.values())
        behind = sorted(h for h, st in drift.items() if int(st.get("version") or 1) < newest)
        items.append({
            "kind": "profile-drift", "severity": "red", "host": None,
            "label": f"{', '.join(behind)} out of date — update Prospector there" if behind
                     else f"Profiles differ across {len(drift)} machines — workers paused",
            "detail": "Workers are paused until then: pull main and restart." if behind
                      else "\n".join(_profile_line(h, st) for h, st in sorted(drift.items()))
                      + "\nMake each machine's TRIAGE_PROFILE file match and restart its workers."})
    for host, code_v in sorted(outdated.items(), key=lambda kv: kv[0] != me):
        items.append({
            "kind": "outdated", "severity": "red", "host": host,
            "label": (f"Action needed: update Prospector on this machine — the store is "
                      f"on v{store_schema}, this Prospector is v{code_v}") if host == me
                     else f"{host}: needs updating · on v{code_v}, store is v{store_schema}",
            "detail": "Pull main and restart Prospector; its workers are paused until then."})
    if down:
        severity = "red" if len(down) == len(lanes) else "amber"
        items.append({"kind": "lanes", "severity": severity, "host": None,
                      "label": f"{len(down)}/{len(lanes)} worker lanes down",
                      "detail": None})
        for host in sorted((set(tripped_by_host) | set(offline_by_host)) - set(outdated)):
            if host in offline_by_host:
                items.append({
                    "kind": "offline", "severity": severity, "host": host,
                    "label": f"{host}: worker offline · silent "
                             f"{_age_label(offline_by_host[host] / 3600)}",
                    "detail": None})
                continue
            rec = next(h for h in health_hosts if str(h["host"]) == host)
            names = tripped_by_host[host]
            entry = (rec.get("lanes") or {}).get(names[0]) or {}
            first = entry.get("tripped") or {}
            detail = "\n".join(str(part) for part in (first.get("reason"), entry.get("remedy")) if part)
            items.append({
                "kind": "trip", "severity": severity, "host": host,
                "label": f"{host}: {', '.join(names)} paused · "
                         f"{first.get('kind') or 'unknown'}",
                "detail": detail or None})

    for h in health_hosts:
        host = str(h["host"])
        disk = _disk_item(host, h.get("disk"), now_ts) if host not in offline_by_host else None
        if disk:
            items.append(disk)

    for key, label in INGEST_LABELS.items():
        item = _ingest_item(label, ingest_last.get(key), now_ts)
        if item:
            items.append(item)

    severity = max((i["severity"] for i in items),
                   key=lambda s: _SEVERITY_RANK[s], default="ok")
    return {"severity": severity, "items": items,
            "lanes_total": len(lanes), "lanes_down": len(down),
            "workers_stalled": bool(lanes) and down == lanes}


def _latest_run(records: list[storekit.RunRecord], phase: str) -> str | None:
    for rec in reversed(records):
        if isinstance(rec, storekit.PhaseRun) and rec.phase == phase:
            return rec.finished or rec.started
    return None


def _pr_ingest_last() -> str | None:
    global _pr_ingest_cache
    now = time.monotonic()
    if _pr_ingest_cache and now - _pr_ingest_cache[0] < _PR_INGEST_TTL_SECONDS:
        return _pr_ingest_cache[1]
    rec = data.latest_run("ingest")
    last = (rec.finished or rec.started) if isinstance(rec, storekit.PhaseRun) else None
    _pr_ingest_cache = (now, last)
    return last


def status() -> SystemHealth:
    """Gather the live inputs and summarize, across every machine on this
    store. The worker registries are one read (`load_worker_registries`)."""
    from prospector_app.backend import alert_data, issues
    regs = data.store().load_worker_registries()
    worker_lanes: list[tuple[str, str]] = []
    for rec in verify_queue.worker_records(regs["verify_worker"]):
        host = rec.get("host")
        if host:
            worker_lanes += [("security", str(host)), ("verify", str(host))]
    for rec in fix_queue.worker_records(regs["fix_worker"]):
        host = rec.get("host")
        if host:
            worker_lanes.append(("fix", str(host)))
    for host in regs["issue_fix_worker"].get("hosts") or {}:
        worker_lanes.append(("issue-fix", str(host)))
    ingest_last: dict[str, str | None] = {
        "pr": _pr_ingest_last(),
        "issues": _latest_run(issues.cached_runs(), "ingest"),
        "alerts": _latest_run(alert_data.runs(), "alert-ingest"),
    }
    health = escalation.health_status(regs)
    from prospector_app.backend import machine_mode
    stood = frozenset(machine_mode.stood_down_in(regs))
    store_v = schema_gate.store_version()
    me = settings.worker_id()
    summary = summarize(health["hosts"], escalation.offline_workers(registries=regs),
                        worker_lanes, ingest_last, stood_down=stood,
                        outdated=schema_gate.outdated_hosts(regs, store_v, me), store_schema=store_v, me=me,
                        profile_drift=profile_gate.drift(regs))
    from prospector_app.backend import capacity_view
    try:
        paused = capacity_view.health_items()
    except Exception:
        paused = []
    for item in paused:
        summary["items"].append(HealthItem(kind=item["kind"], severity=item["severity"],
                                           label=item["label"], detail=item["detail"],
                                           host=item["host"]))
        if _SEVERITY_RANK[item["severity"]] > _SEVERITY_RANK[summary["severity"]]:
            summary["severity"] = item["severity"]
    try:
        threats = threat_view.summary()
    except Exception:
        threats = None  # the strip still answers; the banner waits for the next poll
    if threats is not None:
        summary["threats"] = threats
    return summary
