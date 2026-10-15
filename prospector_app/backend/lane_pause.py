"""Lanes an operator paused on one machine.

A paused lane takes no new work, operator-queued or hunted, including the push
of a change the operator approved; the item in flight finishes. A pause is not
a trip: it books no failure and retests nothing, and it lasts until Resume.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pipeline import worker_health
from prospector_app.backend import data


def _checked(lane: str) -> str:
    if lane not in worker_health.LANES:
        raise ValueError(f"not a worker lane: {lane!r}")
    return lane


def _record(host: str) -> dict:
    rec = data.store().load_lane_pauses().get(host) or {}
    return {"host": host, "lanes": dict(rec.get("lanes") or {})}


def pause(host: str, lane: str, *, by: str) -> dict:
    rec = _record(host)
    rec["lanes"][_checked(lane)] = {"at": datetime.now(timezone.utc).isoformat(), "by": by}
    data.store().save_lane_pause(rec)
    return rec


def resume(host: str, lane: str) -> dict:
    rec = _record(host)
    rec["lanes"].pop(_checked(lane), None)
    data.store().save_lane_pause(rec)
    return rec


def paused_lanes(pauses: dict[str, dict]) -> dict[str, set[str]]:
    return {host: set(rec.get("lanes") or {}) for host, rec in pauses.items()
            if rec.get("lanes")}


def paused(host: str, lane: str, pauses: dict[str, dict] | None = None) -> bool:
    rows = pauses if pauses is not None else data.store().load_lane_pauses()
    return lane in ((rows.get(host) or {}).get("lanes") or {})
