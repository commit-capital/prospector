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
import tempfile
from pathlib import Path

from pipeline import settings
from prospector_app.backend import fix_worker, verify_worker

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"

# The only keys this module may write. Each is a worker lane switch — nothing
# here names a credential, a path, or the store.
WRITABLE = ("TRIAGE_VERIFY_WORKER", "TRIAGE_VERIFY_AUTOHUNT",
            "TRIAGE_FIX_WORKER", "TRIAGE_FIX_AUTOHUNT", "TRIAGE_FIX_AUTOPUSH")


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
    for key in ("TRIAGE_VERIFY_WORKER", "TRIAGE_VERIFY_AUTOHUNT",
                "TRIAGE_FIX_WORKER", "TRIAGE_FIX_AUTOHUNT"):
        if key in clean and clean[key] not in ("", "1"):
            raise ValueError(f"{key} is \"1\" or empty, not {clean[key]!r}")
    return clean


def _rewrite(text: str, updates: dict[str, str]) -> str:
    """`text` with each update applied to its own line, every other line kept
    byte for byte. A key the file does not mention is appended; one it comments
    out is replaced in place, so the commented example in .env.example becomes
    the live setting rather than a duplicate below it."""
    lines = text.splitlines(keepends=True)
    remaining = dict(updates)
    for i, line in enumerate(lines):
        bare = line.lstrip("#").strip()
        key = bare.split("=", 1)[0].strip() if "=" in bare else ""
        if key not in remaining:
            continue
        ending = "\n" if line.endswith("\n") else ""
        lines[i] = f"{key}={remaining.pop(key)}{ending}"
    if remaining:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n# Worker lanes, set from the app's Setup view.\n")
        lines.extend(f"{k}={v}\n" for k, v in remaining.items())
    return "".join(lines)


def set_flags(updates: dict[str, str]) -> dict[str, str]:
    """Write `updates` to `.env` and to this process's environment. Returns the
    flags as they now stand.

    The file is replaced whole from a temporary sibling, so a failed write
    leaves the previous .env intact rather than a half-written one — this file
    holds the store password, and a truncated one costs more than a failed
    toggle."""
    clean = _validated(updates)
    text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    with tempfile.NamedTemporaryFile("w", dir=ENV_PATH.parent, delete=False) as tmp:
        tmp.write(_rewrite(text, clean))
        staged = Path(tmp.name)
    staged.chmod(0o600)
    staged.replace(ENV_PATH)
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
