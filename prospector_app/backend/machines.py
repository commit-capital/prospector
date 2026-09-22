"""The machine roster — every worker machine the shared store knows (#323).

One read composes what the store's registries already record per machine: the
verify/fix heartbeat rows (who is online, what each is working on), the
`worker_health:<id>` lane records (which lanes are tripped), and the verify
base pins. The Control tab's Machines panel projects this, so a deployment
with several worker machines shows all of them, not just the local one.
"""
from __future__ import annotations

from pipeline import settings
from pipeline import worker_health
from prospector_app.backend import data
from prospector_app.backend import verify_queue


def roster() -> dict:
    """Every known machine with its lane health and heartbeats, online first:
    ``{machines: [{host, lanes, beats, online, base_pinned}], local}``."""
    st = data.store()
    machines: dict[str, dict] = {}

    def entry(host: str) -> dict:
        return machines.setdefault(host, {
            "host": host, "lanes": {}, "beats": {},
            "online": False, "base_pinned": False,
        })

    for lane, reg in (("verify", st.load_verify_worker()),
                      ("fix", st.load_fix_worker())):
        for host, rec in (reg.get("hosts") or {}).items():
            e = entry(str(host))
            online = verify_queue.beat_online(rec.get("last_beat"))
            e["beats"][lane] = {
                "last_beat": rec.get("last_beat"),
                "online": online,
                "current_pr": rec.get("current_pr"),
                "autohunt": bool(rec.get("autohunt")),
            }
            e["online"] = e["online"] or online

    for host, rec in (st.load_worker_health().get("hosts") or {}).items():
        e = entry(str(host))
        for lane in worker_health.LANES:
            ln = (rec.get("lanes") or {}).get(lane)
            if ln is None:
                continue
            e["lanes"][lane] = {
                "tripped": bool(ln.get("tripped")),
                "consecutive_failures": ln.get("consecutive_failures") or 0,
                "last_success_at": ln.get("last_success_at"),
            }

    for host in st.load_verify_base_hosts():
        entry(str(host))["base_pinned"] = True

    rows = sorted(machines.values(), key=lambda m: (not m["online"], m["host"]))
    return {"machines": rows, "local": settings.worker_id()}
