"""Turning this machine's worker lanes on and off.

Two operations, both local to this backend: write the worker flags to the repo
root `.env`, and reconcile the running threads with what those flags now say.
The Setup view is the caller; `setup-worker-machine.sh` writes the same keys
through the same allowlist, so the script and the app cannot disagree.

The allowlist is the whole safety story. `.env` also holds TRIAGE_STORE_URL
with its password, the bot PEM's path, and the push key's path; this module
never reads one back to a caller and never writes a key outside WRITABLE. An
unknown key is a hard error rather than a silent skip, so a typo can never look
like it applied.
"""
from __future__ import annotations

import os
import re

from pipeline import settings
from prospector_app.backend import env_file, fix_worker, verify_worker

# The only keys this module may write: the worker lane switches and the name
# the lanes stamp on their work — nothing here names a credential, a path, or
# the store.
WRITABLE = ("TRIAGE_VERIFY_WORKER", "TRIAGE_VERIFY_AUTOHUNT",
            "TRIAGE_FIX_WORKER", "TRIAGE_FIX_AUTOHUNT", "TRIAGE_FIX_HUNT_FIX",
            "TRIAGE_FIX_HUNT_RESOLVE", "TRIAGE_FIX_AUTOPUSH", "TRIAGE_WORKER_ID")

# What a worker id may look like: one token a registry key, a log line, and an
# issue title can all carry verbatim.
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def flags() -> dict[str, str]:
    """The current value of each writable flag, from the live environment."""
    return {k: os.environ.get(k, "") for k in WRITABLE}


def _validated(updates: dict[str, str]) -> dict[str, str]:
    """The updates, proved writable and well-formed. Raises ValueError on a key
    outside the allowlist or a value the settings parser would reject at boot —
    refusing here is what keeps the app from writing a .env that fails to load."""
    unknown = sorted(set(updates) - set(WRITABLE))
    if unknown:
        raise ValueError(f"not a worker flag: {', '.join(unknown)}")
    clean = {k: str(v).strip() for k, v in updates.items()}
    if "TRIAGE_FIX_AUTOPUSH" in clean:
        names = {p.strip().lower() for p in clean["TRIAGE_FIX_AUTOPUSH"].split(",") if p.strip()}
        bad = sorted(names - set(settings.FIX_ACTIONS))
        if bad:
            raise ValueError(f"not an autofix action: {', '.join(bad)}")
    if "TRIAGE_WORKER_ID" in clean and clean["TRIAGE_WORKER_ID"] and not _WORKER_ID.match(clean["TRIAGE_WORKER_ID"]):
        raise ValueError(f"TRIAGE_WORKER_ID is not a usable worker name: {clean['TRIAGE_WORKER_ID']!r}")
    for key in ("TRIAGE_VERIFY_WORKER", "TRIAGE_VERIFY_AUTOHUNT",
                "TRIAGE_FIX_WORKER", "TRIAGE_FIX_AUTOHUNT", "TRIAGE_FIX_HUNT_FIX",
                "TRIAGE_FIX_HUNT_RESOLVE"):
        if key in clean and clean[key] not in ("", "1"):
            raise ValueError(f"{key} is \"1\" or empty, not {clean[key]!r}")
    return clean


def set_flags(updates: dict[str, str]) -> dict[str, str]:
    """Write `updates` to `.env` and to this process's environment. Returns the
    flags as they now stand.

    The allowlist is this module's whole job; env_file owns putting the result
    on disk without disturbing the rest of the file."""
    clean = _validated(updates)
    env_file.write(clean)
    os.environ.update(clean)
    return flags()



def apply() -> dict:
    """Reconcile the running threads with the current flags: start a lane whose
    flag is on and is not running, stop one whose flag is off.

    Each lane is reported on its own, so one refusing to start (an autofix
    worker whose push key fails its safety bar) never hides the other
    succeeding. A stop that outruns SHUTDOWN_TIMEOUT reports `stopping`: the
    loops have been signalled and the run in flight is finishing."""
    out: dict[str, str] = {}
    for name, mod in (("verify", verify_worker), ("fix", fix_worker)):
        if mod.enabled():
            out[name] = "running" if mod.startup() else "refused"
        elif mod.running():
            out[name] = "stopped" if mod.shutdown() else "stopping"
        else:
            out[name] = "off"
    return {"lanes": out, "flags": flags()}
