"""Machines an operator stood down on purpose.

A stood-down machine is expected to be silent: its offline heartbeat and its
tripped lanes are not problems. The mark lasts until any worker on that
machine beats after it, so a machine that comes back reads as working again.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pipeline import storekit
from prospector_app.backend import data

if TYPE_CHECKING:
    from pipeline.store import WorkerRegistries


def stand_down(host: str, *, by: str, note: str | None = None,
               now: datetime | None = None) -> dict:
    record = {"host": host, "stood_down": True,
              "at": (now or datetime.now(timezone.utc)).isoformat(),
              "by": by, "note": note}
    data.store().save_machine_mode(record)
    return record


def stand_up(host: str) -> bool:
    return data.store().clear_machine_mode(host)


def stood_down(modes: dict[str, dict], roster: dict) -> dict[str, dict]:
    beats: dict[str, list[datetime]] = {}
    for m in roster.get("machines") or []:
        stamps = [storekit.parse_ts(b.get("last_beat")) for b in (m.get("beats") or {}).values()]
        beats[str(m["host"])] = [s for s in stamps if s is not None]
    out: dict[str, dict] = {}
    for host, mark in modes.items():
        at = storekit.parse_ts(mark.get("at"))
        if not mark.get("stood_down") or at is None:
            continue
        if any(b > at for b in beats.get(host, [])):
            continue
        out[host] = mark
    return out


def stood_down_in(regs: WorkerRegistries) -> dict[str, dict]:
    """`stood_down` over one `Store.load_worker_registries` read."""
    machines: dict[str, dict] = {}
    for lane, reg in (("verify", regs["verify_worker"]), ("fix", regs["fix_worker"]),
                      ("issue-fix", regs["issue_fix_worker"])):
        for host, rec in (reg.get("hosts") or {}).items():
            machines.setdefault(str(host), {"host": str(host), "beats": {}})["beats"][lane] = rec
    return stood_down(regs["machine_modes"], {"machines": list(machines.values())})
