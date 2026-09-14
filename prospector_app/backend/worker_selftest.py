"""What a tripped lane runs to find out whether it may open again.

Each test answers the condition that tripped the lane, cheaply and without
touching any PR: an agent outage asks the CLI for one word; anything else asks
whether the sandbox this machine verifies and compiles in can still boot. A
test that raises is a failed test.
"""
from __future__ import annotations

from pipeline import headless_agent, verify_driver
from prospector_app.backend import data

# Trip kinds that mean the agent CLI, not the sandbox, is what failed.
AGENT_KINDS = frozenset({"agent-unavailable"})


def sandbox_ready() -> str | None:
    """None when the Docker daemon answers and this machine's pinned base
    image is present, else the first thing missing."""
    if not verify_driver.daemon_available():
        return "the Docker daemon is not answering"
    pin = verify_driver.local_pin(data.store())
    sha, tier = pin.get("base_sha"), pin.get("tier")
    if not sha or tier is None:
        return "this machine has no pinned base"
    image = verify_driver.base_image_tag(str(sha), int(tier))
    if not verify_driver.image_exists(image):
        return f"the pinned base image {image} is not in the local Docker daemon"
    if not verify_driver.base_clone_dir(str(sha)).is_dir():
        return f"the pinned base clone for {str(sha)[:12]} is missing"
    return None


def run(kind: str) -> str | None:
    """The self-test for a lane tripped by `kind`: None on a pass, else why
    the lane must stay closed."""
    try:
        if kind in AGENT_KINDS:
            return headless_agent.probe()
        return sandbox_ready()
    except Exception as e:  # a test that cannot answer is not a pass
        return f"{type(e).__name__}: {e}"
