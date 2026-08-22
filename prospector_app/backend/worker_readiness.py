"""What this machine still needs before it can process work.

The ONE answer to "why is this machine not verifying", shared by the Setup view
and by `setup-worker-machine.sh`, so the script and the app can never disagree
about what ready means. Every check is read-only and side-effect free, which is
what lets the view poll it while the script is mid-run.

A check reports what it found and the remedy for what it did not. A check that
raises reports as failing with the exception as its detail: a check that cannot
answer is not evidence the machine is ready.
"""
from __future__ import annotations

import shutil
import socket
from collections.abc import Callable
from typing import TypedDict

from pipeline import settings, verify_driver
from prospector_app.backend import data, fix_worker, verify_worker


class Check(TypedDict):
    key: str
    label: str
    ok: bool
    detail: str
    remedy: str | None
    # Whether a machine that fails this one can still process work at all. A
    # missing push identity blocks autofix and nothing else, so the view can
    # show it as a limit rather than a fault.
    blocking: bool


def _docker_daemon() -> tuple[bool, str, str]:
    if shutil.which("docker") is None:
        return False, "no docker binary on PATH", "install a Docker runtime"
    if not verify_driver.daemon_available():
        return False, "the daemon is not answering", "start it (e.g. `colima start`)"
    return True, "answering", ""


def _sandbox_image() -> tuple[bool, str, str]:
    tag = verify_driver.sandbox_image()
    if not verify_driver.daemon_available():
        return False, "cannot look: the Docker daemon is not answering", "start Docker"
    if not verify_driver.image_exists(tag):
        others = [t for t in verify_driver.sandbox_images() if t != tag]
        if others:
            # Naming the image already here tells the operator this is a rebuild.
            return False, (f"{tag} is not built; this machine has {', '.join(others)}, "
                           f"built for a different pnpm pin"), "run build-image here"
        return False, f"{tag} is not built", "run build-image here"
    return True, f"{tag} present", ""


def _base_pin() -> tuple[bool, str, str]:
    """This machine's own pin, plus the two artifacts it names on local disk."""
    pin = verify_driver.local_pin(data.store())
    sha, tier = pin.get("base_sha"), pin.get("tier")
    if not sha or tier is None:
        return False, "this machine has pinned no base", "run prepare-base here"
    clone = verify_driver.base_clone_dir(str(sha))
    if not clone.is_dir():
        return False, f"pinned {str(sha)[:12]} but its clone is missing from {clone}", \
            "run prepare-base here"
    if not verify_driver.daemon_available():
        return False, "cannot look for the base image: the Docker daemon is not answering", \
            "start Docker"
    image = verify_driver.base_image_tag(str(sha), int(tier))
    if not verify_driver.image_exists(image):
        return False, f"pinned {str(sha)[:12]} but {image} is not in the local daemon", \
            "run prepare-base here"
    return True, f"{str(sha)[:12]} at tier {tier}", ""


def _push_identity() -> tuple[bool, str, str]:
    if not settings.push_identity_configured():
        return False, "no contributor-push identity", \
            "set TRIAGE_PUSH_LOGIN, TRIAGE_PUSH_EMAIL and TRIAGE_PUSH_SSH_KEY_FILE"
    failure = fix_worker.key_safety_failure()
    if failure is not None:
        return False, failure, "fix the key, then restart the worker"
    return True, f"pushes as {settings.PUSH_LOGIN}", ""


def _verify_flag() -> tuple[bool, str, str]:
    if not verify_worker.enabled():
        return False, "TRIAGE_VERIFY_WORKER is not set", "turn the verify worker on"
    if not verify_worker.running():
        return False, "opted in, but no worker threads are live", "start the verify worker"
    return True, "draining the verify queue", ""


def _fix_flag() -> tuple[bool, str, str]:
    if not fix_worker.enabled():
        return False, "TRIAGE_FIX_WORKER is not set", "turn the autofix worker on"
    if not fix_worker.running():
        return False, "opted in, but no worker threads are live", "start the autofix worker"
    return True, "draining the autofix queue", ""


# key, label, probe, blocking. Ordered the way a machine is provisioned, so the
# first failing row is the one to act on.
_CHECKS: list[tuple[str, str, Callable[[], tuple[bool, str, str]], bool]] = [
    ("docker", "Docker daemon", _docker_daemon, True),
    ("sandbox_image", "Hardened sandbox image", _sandbox_image, True),
    ("base_pin", "Pinned base", _base_pin, True),
    ("verify_flag", "Verify worker", _verify_flag, True),
    ("push_identity", "Contributor-push identity", _push_identity, False),
    ("fix_flag", "Autofix worker", _fix_flag, False),
]


def checks() -> list[Check]:
    """Every readiness check for this machine, in provisioning order."""
    out: list[Check] = []
    for key, label, probe, blocking in _CHECKS:
        try:
            ok, detail, remedy = probe()
        except Exception as e:
            ok, detail, remedy = False, f"{type(e).__name__}: {e}", "see the detail"
        out.append({"key": key, "label": label, "ok": ok, "detail": detail,
                    "remedy": remedy or None, "blocking": blocking})
    return out


def report() -> dict:
    """This machine's readiness, for the Setup view and the script's preflight.
    `ready` means it can process verification work; autofix is reported
    separately because a machine may deliberately run only one lane."""
    rows = checks()
    by_key = {c["key"]: c for c in rows}
    return {
        "host": socket.gethostname(),
        "checks": rows,
        "ready": all(c["ok"] for c in rows if c["blocking"]),
        "autofix_ready": by_key["push_identity"]["ok"] and by_key["fix_flag"]["ok"],
    }
