"""Telling a human that a worker has stopped working properly.

A tripped lane and a worker that stopped beating are the two conditions the
system cannot fix on its own. Each is escalated the same three ways: a
`worker:trip` or `worker:offline` entry in the runs ledger, the health record
the Control tab's banner reads, and an issue on PROSPECTOR_FEEDBACK_REPO filed
as the operator (the only identity that can reach the meta-repo), labeled so it
can be filtered, and deduplicated per failure signature for a week.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from pipeline import gh, settings, worker_health
from pipeline.storekit import now as _now
from prospector_app.backend import data, verify_queue, worker_log

LABEL = "worker-health"
LABEL_COLOR = "D93F0B"

# How often any live backend looks for workers that stopped beating.
WATCH_SECONDS = 600.0

_watch_thread: threading.Thread | None = None
_stop = threading.Event()


def _issue_number(url: str) -> int | None:
    m = re.search(r"/issues/(\d+)", url)
    return int(m.group(1)) if m else None


def file_issue(title: str, body: str) -> tuple[int | None, str | None]:
    """Create the issue on the feedback repo as the operator. Returns (number,
    url), or (None, None) when no repo is configured or gh could not file it.
    Never raises: escalation must not take the worker down with it."""
    repo = settings.feedback_repo()
    if not repo:
        return None, None
    env = gh.operator_env()
    try:
        subprocess.run(["gh", "label", "create", LABEL, "--repo", repo, "--force",
                        "--color", LABEL_COLOR,
                        "--description", "Filed by Prospector when a worker trips or goes dark"],
                       capture_output=True, text=True, timeout=30, env=env)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write(body)
            body_path = fh.name
        try:
            for labels in (["--label", LABEL], []):
                r = subprocess.run(["gh", "issue", "create", "--repo", repo, "--title", title,
                                    "--body-file", body_path, *labels],
                                   capture_output=True, text=True, timeout=60, env=env)
                if r.returncode == 0:
                    url = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
                    return _issue_number(url), url or None
            print(f"[escalation] could not file issue on {repo}: "
                  f"{(r.stderr or r.stdout).strip()[:300]}", flush=True)
        finally:
            Path(body_path).unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[escalation] could not file issue on {repo}: {e}", flush=True)
    return None, None


def _trip_body(host: str, lane: str, entry: dict) -> str:
    tripped = entry.get("tripped") or {}
    recent = entry.get("recent") or []
    lines = [
        f"Prospector's **{lane}** lane on worker **{host}** tripped and stopped picking work.",
        "",
        f"- kind: `{tripped.get('kind')}`",
        f"- reason: {tripped.get('reason')}",
        f"- tripped at: {tripped.get('at')}",
        f"- repository: {settings.repo()}",
        "",
        "The lane retests itself every 15 minutes and reopens on a pass; the Control tab's "
        "banner has a Resume button for a manual override.",
        "",
        "### Last machine failures",
    ]
    for f in recent[-worker_health.RECENT_KEEP:]:
        pr = f" (PR #{f.get('pr')})" if f.get("pr") else ""
        lines.append(f"- {f.get('at')} `{f.get('kind')}`{pr}: {f.get('reason')}")
    tail = worker_log.tail(3000)
    if tail:
        lines += ["", "### Worker log tail", "```", tail.strip(), "```"]
    lines += ["", "_Filed automatically by prospector_app/backend/escalation.py._"]
    return "\n".join(lines)


def escalate_trip(lane: str) -> None:
    """Record this worker's fresh trip of `lane` in the ledger and file (or
    skip, when one is recent for the same signature) the issue."""
    host = settings.worker_id()
    st = data.store()
    rec = worker_health.load(st, host)
    entry = worker_health.lane(rec, lane)
    tripped = entry.get("tripped") or {}
    kind = str(tripped.get("kind") or "unknown")
    reason = str(tripped.get("reason") or "")
    try:
        st.append_run({"phase": "worker:trip", "started": _now(), "finished": _now(),
                       "stats": {"host": host, "lane": lane, "kind": kind,
                                 "reason": reason[:600]}})
    except Exception:
        traceback.print_exc()
    sig = worker_health.signature(kind, reason)
    if not worker_health.issue_due(rec, lane, sig):
        print(f"[escalation] {lane} lane tripped on {host}; an issue for this failure "
              f"is already open", flush=True)
        return
    number, url = file_issue(
        f"[worker-health] {host}: {lane} lane tripped ({kind})",
        _trip_body(host, lane, entry))
    worker_health.update(st, host, lambda r: worker_health.record_issue(
        r, lane, sig=sig, number=number, url=url))
    print(f"[escalation] {lane} lane tripped on {host}: {reason[:200]}"
          + (f" — filed {url}" if url else " — no issue filed"), flush=True)


def offline_workers(now: datetime | None = None) -> list[dict]:
    """Every worker whose last heartbeat, in either lane's registry, is older
    than OFFLINE_AFTER_SECONDS: `{host, lane, last_beat, age_seconds}`."""
    now = now or datetime.now(timezone.utc)
    st = data.store()
    out: list[dict] = []
    for lane, reg in (("verify", st.load_verify_worker()),
                      ("fix", st.load_fix_worker())):
        for host, r in (reg.get("hosts") or {}).items():
            last = r.get("last_beat")
            if verify_queue.beat_online(last):
                continue
            try:
                age = (now - datetime.fromisoformat(str(last))).total_seconds()
            except ValueError:
                continue
            if age >= worker_health.OFFLINE_AFTER_SECONDS:
                out.append({"host": host, "lane": lane, "last_beat": last,
                            "age_seconds": age})
    return out


def check_offline_workers(now: datetime | None = None) -> list[str]:
    """Escalate each worker that has gone dark, once per silence: the ledger
    entry and the issue key on the worker and the beat it went dark after.
    Returns the hosts escalated this pass."""
    st = data.store()
    escalated: list[str] = []
    seen: set[str] = set()
    for w in offline_workers(now):
        host = str(w["host"])
        if host in seen:
            continue
        seen.add(host)
        rec = worker_health.load(st, host)
        sig = worker_health.signature("offline", f"since {w['last_beat']}")
        if not worker_health.issue_due(rec, "worker", sig, now):
            continue
        hours = float(w["age_seconds"]) / 3600
        reason = (f"no heartbeat from {host} since {w['last_beat']} "
                  f"({hours:.1f} hours)")
        try:
            st.append_run({"phase": "worker:offline", "started": _now(), "finished": _now(),
                           "stats": {"host": host, "reason": reason}})
        except Exception:
            traceback.print_exc()
        body = "\n".join([
            f"Prospector worker **{host}** stopped beating.",
            "",
            f"- last heartbeat: {w['last_beat']} ({hours:.1f} hours ago)",
            f"- repository: {settings.repo()}",
            "",
            "Work it had claimed is reclaimable by any other worker once its heartbeat is "
            "stale. Bring the machine back, or clear its registry entries if it is retired.",
            "",
            "_Filed automatically by prospector_app/backend/escalation.py._"])
        number, url = file_issue(f"[worker-health] {host}: worker offline", body)
        worker_health.update(st, host, lambda r: worker_health.record_issue(
            r, "worker", sig=sig, number=number, url=url))
        print(f"[escalation] {reason}" + (f" — filed {url}" if url else ""), flush=True)
        escalated.append(host)
    return escalated


def _watch_loop() -> None:
    while not _stop.is_set():
        try:
            check_offline_workers()
        except Exception:
            traceback.print_exc()
        _stop.wait(WATCH_SECONDS)


def start_watch() -> bool:
    """Start the offline-worker watch on this backend when a feedback repo is
    configured to escalate into. Idempotent."""
    global _watch_thread
    if not settings.feedback_repo():
        return False
    if _watch_thread is not None and _watch_thread.is_alive():
        return True
    _stop.clear()
    _watch_thread = threading.Thread(target=_watch_loop, daemon=True,
                                     name="escalation-watch")
    _watch_thread.start()
    return True


def health_status() -> dict:
    """Every worker's lane health for the app: `{hosts: [{host, lanes}]}`,
    tripped lanes first."""
    hosts = data.store().load_worker_health().get("hosts") or {}
    out = []
    for host, rec in sorted(hosts.items()):
        lanes = {name: {k: entry.get(k) for k in
                        ("consecutive_failures", "tripped", "retest", "issue",
                         "recent", "last_success_at")}
                 for name, entry in (rec.get("lanes") or {}).items()}
        out.append({"host": host, "lanes": lanes,
                    "tripped": sorted(n for n, e in lanes.items() if e.get("tripped"))})
    out.sort(key=lambda h: (not h["tripped"], h["host"]))
    return {"hosts": out, "any_tripped": any(h["tripped"] for h in out)}


def resume(host: str, lane: str) -> dict:
    """An operator's Resume: reopen the lane. Raises ValueError on an unknown
    lane."""
    if lane not in worker_health.LANES:
        raise ValueError(f"unknown lane {lane!r}")
    rec = worker_health.update(data.store(), host, lambda r: worker_health.reopen(
        r, lane, by="resumed by the operator"))
    try:
        data.store().append_run({"phase": "worker:resume", "started": _now(),
                                 "finished": _now(),
                                 "stats": {"host": host, "lane": lane}})
    except Exception:
        traceback.print_exc()
    return json.loads(json.dumps(rec))
