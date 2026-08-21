"""A configured deployment's write target cannot change under it.

Settings are read live, so this is not a property of Python binding — it is a
property of the write surface. Only two code paths mutate the environment at
runtime, and neither can reach TRIAGE_REPO or TRIAGE_STORE_URL once a
deployment is configured. The executor leans on that: it resolves the
repository afresh at each step of a write, which is safe exactly because no
caller can move it in between.
"""
from __future__ import annotations

import pytest

from prospector_app.backend import onboarding, worker_control


def test_the_lane_writer_cannot_reach_the_deployment_target():
    for key in ("TRIAGE_REPO", "TRIAGE_STORE_URL", "TRIAGE_BOT_LOGIN",
                "TRIAGE_BOT_KEY_FILE"):
        assert key not in worker_control.WRITABLE


@pytest.mark.parametrize("key", ["TRIAGE_REPO", "TRIAGE_STORE_URL"])
def test_no_step_can_move_the_target_once_configured(key, monkeypatch):
    monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
    for step in onboarding.STEP_KEYS:
        with pytest.raises(ValueError):
            onboarding.apply(step, {key: "attacker/repo"}, None)


def test_the_target_lives_only_in_steps_that_close_once_configured():
    for key in ("TRIAGE_REPO", "TRIAGE_STORE_URL"):
        holders = {s for s, keys in onboarding.STEP_KEYS.items() if key in keys}
        assert holders == set(onboarding.CLOSED_ONCE_CONFIGURED)
