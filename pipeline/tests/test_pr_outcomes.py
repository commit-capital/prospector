from __future__ import annotations

import pytest

from pipeline import gates, outcomes, pr_outcomes

HEAD = "h" * 40


@pytest.mark.parametrize("stage, fault", [
    ("reviewed", None), ("blocking", "judged"), ("coverage", "machine"), ("agent", "machine")])
def test_a_design_review_s_fault_follows_its_stage(stage, fault):
    got = pr_outcomes.design(7, HEAD, stage, "words")
    assert (got["lane"], got["stage"], got["fault"]) == ("design-review", stage, fault)
    assert got["subject"] == {"kind": "pr", "number": 7, "sha": HEAD}


@pytest.mark.parametrize("stage, fault", [
    ("green", None), ("red", "judged"), ("held", "machine"), ("no-diff", "machine")])
def test_a_security_review_s_fault_follows_its_verdict(stage, fault):
    assert pr_outcomes.security(7, HEAD, stage)["fault"] == fault


@pytest.mark.parametrize("status, kind, outcome, stage, fault", [
    ("done", None, "verified-fix", "verified", None),
    ("done", None, "agent-verified", "verified", None),
    ("done", None, "not-verified", "not-verified", "judged"),
    ("done", None, "unverifiable-no-test", "unverifiable-no-test", "subject"),
    ("done", None, "deps-touched", "deps-touched", "subject"),
    ("error", "sandbox-error", None, "sandbox-error", "machine"),
    ("queued", "fetch-error", None, "fetch-error", "machine"),
    ("error", "refused-safety", None, "refused-safety", "subject"),
])
def test_a_verify_run_s_stage_is_its_error_kind_or_its_conclusion(status, kind, outcome,
                                                                  stage, fault):
    got = pr_outcomes.verify(7, HEAD, status=status, error_kind=kind, error="e",
                             outcome=outcome)
    assert got is not None and (got["stage"], got["fault"]) == (stage, fault)


def test_a_cancelled_verify_has_no_outcome():
    assert pr_outcomes.verify(7, HEAD, status="cancelled", error_kind=None, error=None,
                              outcome=None) is None


@pytest.mark.parametrize("status, kind, judged, stage, fault", [
    ("pushed", None, None, "pushed", None),
    ("awaiting-review", None, None, "parked", None),
    ("refused", None, True, "refused", "judged"),
    ("refused", None, False, "refused", "subject"),
    ("cancelled", None, True, "cancelled", "judged"),
    ("cancelled", None, None, "cancelled", "subject"),
    ("failed", "run-failed", None, "run-failed", "machine"),
    ("failed", "base-compile", None, "run-failed", "machine"),
    ("failed", "agent-unavailable", None, "agent-unavailable", "machine"),
])
def test_an_autofix_ending_maps_to_a_stage(status, kind, judged, stage, fault):
    got = pr_outcomes.autofix(7, HEAD, action="rebase", status=status, detail="d",
                              kind=kind, judged=judged)
    assert got is not None and (got["stage"], got["fault"]) == (stage, fault)
    assert got["words"] == "rebase: d"


def test_a_capacity_deferral_is_no_outcome():
    assert pr_outcomes.autofix(7, HEAD, action="fix", status="failed", detail=None,
                               kind="capacity-paused", judged=None) is None


def test_every_verify_ending_has_a_stage():
    assert set(gates.VERIFY_OUTCOMES) - {"verified-fix", "agent-verified"} <= set(
        outcomes.STAGES["verify"])
    assert set(gates.VERIFY_REQUEST_FAULT) <= set(outcomes.STAGES["verify"])
