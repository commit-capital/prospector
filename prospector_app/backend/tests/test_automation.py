"""automation.classify: one standing per open PR, in the columns Home shows."""
from __future__ import annotations

import pytest

from pipeline import reviewers
from pipeline import store as S
from pipeline.storekit import now as _now
from pipeline.testsupport import bump_section, greptile_entry, reviews_section
from prospector_app.backend import automation, data, service

HEAD = "a" * 40


def _rec(n: int = 1, *, ci: str = "passing", mergeable: bool = True, greptile: int = 5,
         reviewed_sha: str | None = HEAD, drift: str = "applicable") -> dict:
    return {"pr": n,
            "meta": {"title": "t", "author": "al", "state": "open", "head_sha": HEAD,
                     "updated_at": "2026-06-01T00:00:00+00:00"},
            "signals": {"ci": ci, "mergeable": mergeable, "checked_at": _now(),
                        "against_head_sha": HEAD},
            "reviews": reviews_section(HEAD, _now(), greptile=greptile_entry(greptile, reviewed_sha)),
            "drift": {"state": drift, "checked_at": _now(), "against_head_sha": HEAD}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setattr(service, "changed_paths", lambda pr: ["src/a.ts"])
    monkeypatch.setenv("TRIAGE_FIX_HUNT_FIX", "1")
    monkeypatch.delenv("TRIAGE_FIX_HUNT_SECURITY", raising=False)
    return st


def _classify(store, rec: dict) -> dict:
    store.save_pr(rec)
    data.refresh()
    out = automation.classify(store.load_pr(rec["pr"]))
    assert out is not None
    return out


def test_a_parked_change_is_your_move(store):
    rec = _rec()
    rec["fix_request"] = {"action": "resolve", "status": "awaiting-review", "source": "auto",
                          "against_head_sha": HEAD, "host": "w", "queued_at": _now()}
    out = _classify(store, rec)
    assert (out["column"], out["bucket"], out["owner"]) == ("act", "approve-parked", "you")


def test_a_needs_human_pick_is_handed_to_you(store):
    rec = _rec()
    rec["analysis"] = {"disposition": "needs-human", "rationale": "rewrites the storage layer",
                       "against_head_sha": HEAD, "checked_at": _now()}
    out = _classify(store, rec)
    assert (out["column"], out["bucket"], out["owner"]) == ("handed", "needs-human", "you")


def test_a_queued_fix_is_in_motion(store):
    rec = _rec()
    rec["fix_request"] = {"action": "fix", "status": "queued", "source": "auto",
                          "against_head_sha": HEAD, "host": "w", "queued_at": _now()}
    out = _classify(store, rec)
    assert (out["column"], out["bucket"]) == ("auto", "queued")


def test_a_review_below_the_bar_is_the_hunter_s_next_pick(store, monkeypatch):
    from pipeline import profile
    monkeypatch.setattr(profile, "active", lambda: profile.RepoProfile(
        autofix=profile.AutofixPolicy(fixable_gates=("review",))))
    out = _classify(store, _rec(greptile=4))
    assert (out["column"], out["bucket"], out["owner"]) == ("auto", "hunt", "worker")
    assert "fix" in out["reason"]


def test_a_stale_verdict_waits_on_the_reviewer(store):
    out = _classify(store, _rec(greptile=4, reviewed_sha="0" * 40))
    assert (out["column"], out["bucket"]) == ("auto", "waiting")
    assert "reviewer" in out["reason"]


def test_an_exempt_bump_is_yours_to_merge(store):
    out = _classify(store, {**_rec(mergeable=False), "threat": bump_section(HEAD)})
    assert (out["column"], out["owner"]) == ("handed", "you")


def test_red_ci_at_the_author_s_head_is_the_author_s(store):
    out = _classify(store, _rec(ci="failing", greptile=4))
    assert (out["column"], out["bucket"], out["owner"]) == ("handed", "author-ci", "author")


def test_a_head_the_required_checks_never_ran_on_is_handed_back_not_merge_ready(store):
    out = _classify(store, _rec(ci="unreported", greptile=4))
    assert (out["column"], out["bucket"]) == ("handed", "author-ci")
    assert "update the branch" in out["reason"]


def test_a_head_the_required_checks_never_ran_on_at_the_bar_is_the_hunter_s_update(store):
    out = _classify(store, _rec(ci="unreported"))
    assert (out["column"], out["bucket"]) == ("auto", "hunt")
    assert "update" in out["reason"]


def test_an_update_already_spent_on_this_head_rests(store):
    rec = _rec(ci="unreported")
    rec["fix_request"] = {"action": "update", "status": "refused", "source": "auto",
                          "against_head_sha": HEAD, "host": "w", "queued_at": _now(),
                          "refused_reason": "the update did not compile"}
    out = _classify(store, rec)
    assert out["bucket"] != "hunt"


def test_a_head_whose_workflow_runs_await_approval_is_your_click(store):
    out = _classify(store, _rec(ci="awaiting-approval"))
    assert (out["column"], out["bucket"], out["owner"]) == ("act", "approve-ci", "you")
    assert "approve the workflow run" in out["reason"]


def test_a_refusal_the_agent_declined_is_handed_to_the_author_with_its_reasoning(store):
    rec = _rec(greptile=4)
    rec["fix_request"] = {"action": "fix", "status": "refused", "source": "auto",
                          "against_head_sha": HEAD, "host": "w", "queued_at": _now(),
                          "refused_reason": "The agent declined to write a change: needs a product decision"}
    out = _classify(store, rec)
    assert (out["column"], out["bucket"], out["owner"]) == ("handed", "author-declined", "author")
    assert "product decision" in out["reason"]


def test_a_conflicted_pr_below_the_review_bar_is_the_author_s_rebase(store):
    out = _classify(store, _rec(mergeable=False, greptile=3))
    assert (out["column"], out["bucket"], out["owner"]) == ("handed", "author-conflicts", "author")


def test_a_conflicted_pr_at_the_bar_is_the_hunter_s_rebase(store):
    out = _classify(store, _rec(mergeable=False, greptile=5))
    assert (out["column"], out["bucket"]) == ("auto", "hunt")
    assert "rebase" in out["reason"]


def test_a_bot_pushed_head_whose_ci_fails_is_the_bot_s_to_repair(store, monkeypatch):
    from pipeline import profile
    monkeypatch.setattr(profile, "active", lambda: profile.RepoProfile(
        autofix=profile.AutofixPolicy(fixable_gates=("objection",))))
    rec = _rec(ci="failing")
    rec["fix_request"] = {"action": "fix", "status": "pushed", "source": "auto",
                          "against_head_sha": "b" * 40, "host": "w", "queued_at": _now(),
                          "result": {"pushed_head_sha": HEAD}}
    out = _classify(store, rec)
    assert (out["column"], out["bucket"]) == ("auto", "hunt")
    assert "CI this bot broke" in out["reason"]


def test_a_closed_pr_has_no_standing(store):
    rec = _rec(); rec["meta"]["state"] = "closed"
    store.save_pr(rec); data.refresh()
    assert automation.classify(store.load_pr(1)) is None


def test_the_row_carries_it_and_the_filters_read_it(store):
    rec = _rec(ci="failing", greptile=4)
    store.save_pr(rec); data.refresh()
    row = service.pr_row(1)
    assert row is not None and row["automation"]["bucket"] == "author-ci"
    from prospector_app.backend import filters
    assert filters.matches(row, {"automation_column": "handed"})
    assert filters.matches(row, {"automation_bucket": ["author-ci", "author-declined"]})
    assert filters.matches(row, {"automation_owner": "author"})
    assert not filters.matches(row, {"automation_column": "auto"})


def test_a_conflicted_pr_with_a_stale_verdict_is_the_author_s_rebase(store):
    out = _classify(store, _rec(mergeable=False, greptile=5, reviewed_sha="0" * 40))
    assert (out["column"], out["bucket"], out["owner"]) == ("handed", "author-conflicts", "author")
    assert "rebase" in out["reason"]


def _merge_pick(rec: dict, *, verify: dict | None = None, verify_request: dict | None = None) -> dict:
    rec["analysis"] = {"disposition": "merge", "rationale": "clean", "against_head_sha": HEAD,
                       "checked_at": _now()}
    rec["security"] = {"verdict": "GREEN", "checked_at": _now(), "against_head_sha": HEAD, "findings": []}
    rec["threat"] = {"verdict": "clear", "checked_at": _now(), "against_head_sha": HEAD}
    rec["signals"]["has_tests"] = True
    if verify is not None:
        rec["verify"] = verify
    if verify_request is not None:
        rec["verify_request"] = verify_request
    return rec


def test_ready_means_every_check_is_clear(store, monkeypatch):
    from pipeline import gates
    monkeypatch.setattr(gates, "verify_signals_incomplete", lambda pr: None)
    rec = _merge_pick(_rec(), verify={"outcome": "verified-fix", "checked_at": _now(),
                                      "against_head_sha": HEAD, "against_base_sha": "b" * 40,
                                      "signals": {}})
    out = _classify(store, rec)
    assert (out["column"], out["bucket"]) == ("act", "merge-ready")


@pytest.mark.parametrize("design_sha", [HEAD, "b" * 40])
def test_an_advisory_design_review_never_holds_a_ready_pick(store, monkeypatch, design_sha):
    from pipeline import gates, review_policy
    monkeypatch.setattr(gates, "verify_signals_incomplete", lambda pr: None)
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "advisory")
    review_policy.reset()
    rec = _merge_pick(_rec(), verify={"outcome": "verified-fix", "checked_at": _now(),
                                      "against_head_sha": HEAD, "against_base_sha": "b" * 40,
                                      "signals": {}})
    rec["reviews"]["design"] = {"kind": "review", "reviewed_sha": design_sha, "checks": [],
                                "extra": {"rubric": reviewers.DESIGN_RUBRIC}, "findings": [{"severity": "blocking", "verification": {"verdict": "confirmed", "reason": "r"},
                                                           "resolved": False, "outdated": False}]}
    out = _classify(store, rec)
    assert (out["column"], out["bucket"]) == ("act", "merge-ready")


def test_a_ready_pick_whose_head_the_threat_scan_has_not_judged_waits_on_it(
        store, monkeypatch):
    from pipeline import gates
    monkeypatch.setattr(gates, "verify_signals_incomplete", lambda pr: None)
    rec = _merge_pick(_rec(), verify={"outcome": "verified-fix", "checked_at": _now(),
                                      "against_head_sha": HEAD, "against_base_sha": "b" * 40,
                                      "signals": {}})
    rec["threat"]["against_head_sha"] = "0" * 40
    out = _classify(store, rec)
    assert (out["column"], out["bucket"], out["owner"]) == ("auto", "waiting", "worker")
    assert "threat scan" in out["reason"]


def test_a_diff_the_threat_scan_could_not_read_is_your_call(store):
    rec = _merge_pick(_rec())
    rec["threat"] = {"verdict": "suspicious", "signatures": ["unscannable-diff"],
                     "detail": {"unscannable-diff": "not read: dist/huge.js"},
                     "checked_at": _now(), "against_head_sha": HEAD}
    out = _classify(store, rec)
    assert (out["column"], out["bucket"], out["owner"]) == ("handed", "other", "you")
    assert "dist/huge.js" in out["reason"]


def test_partial_verification_is_your_judgment_not_ready(store):
    rec = _merge_pick(_rec(), verify={"outcome": "verified-fix", "checked_at": _now(),
                                      "against_head_sha": HEAD, "against_base_sha": "b" * 40,
                                      "signals": {}})
    out = _classify(store, rec)
    assert (out["column"], out["bucket"], out["owner"]) == ("handed", "other", "you")
    assert "partial" in out["reason"]


def test_a_merge_pick_whose_verification_never_ran_is_in_motion_not_ready(store):
    out = _classify(store, _merge_pick(_rec()))
    assert (out["column"], out["bucket"]) == ("auto", "waiting")
    assert "verification has not run" in out["reason"]


def test_a_clean_reviewed_pr_analyze_never_reached_waits_on_analysis_not_ready(store):
    rec = _merge_pick(_rec())
    del rec["analysis"]
    out = _classify(store, rec)
    assert (out["column"], out["bucket"], out["owner"]) == ("auto", "waiting", "worker")
    assert "not analyzed" in out["reason"]


def test_a_merge_eligible_pr_on_a_codeowners_path_is_handed_to_you(store, monkeypatch):
    from pipeline import profile
    monkeypatch.setattr(profile, "active", lambda: profile.RepoProfile(
        codeowners=profile.CodeownersPolicy(gated_globs=("skills/**",), owners=("@o",))))
    monkeypatch.setattr(service, "changed_paths", lambda pr: ["skills/foo.md"])
    rec = _merge_pick(_rec())
    del rec["analysis"]
    out = _classify(store, rec)
    assert (out["column"], out["bucket"], out["owner"]) == ("handed", "gated", "you")
    assert "skills/foo.md" in out["reason"]


def test_an_unanalyzed_pr_without_a_security_review_waits_on_the_hunter(store):
    rec = _merge_pick(_rec())
    del rec["analysis"], rec["security"]
    out = _classify(store, rec)
    assert (out["column"], out["bucket"]) == ("auto", "waiting")
    assert "security hunter" in out["reason"]


def test_a_merge_pick_whose_verification_hit_a_machine_fault_is_in_motion(store):
    rec = _merge_pick(_rec(), verify_request={
        "status": "error", "error_kind": "base-lane", "queued_at": _now(),
        "finished_at": _now(), "host": "elsewhere", "against_head_sha": HEAD,
        "error": "The repository's own build command fails on master itself"})
    out = _classify(store, rec)
    assert out["column"] != "act"
    assert out["bucket"] in ("waiting", "other")
    assert "verification" in out["reason"]


@pytest.mark.parametrize("over", [{}, {"mergeable": False}, {"ci": "failing"},
                                  {"reviewed_sha": "0" * 40}])
def test_a_malicious_pr_is_your_move_whatever_else_holds(store, over):
    rec = _rec(**over)
    rec["threat"] = {"verdict": "malicious", "signatures": ["obfuscated-payload"],
                     "checked_at": _now(), "against_head_sha": HEAD}
    out = _classify(store, rec)
    assert (out["column"], out["bucket"], out["owner"]) == ("act", "malicious", "you")
    assert "obfuscated-payload" in out["reason"]


def test_a_merge_pick_whose_head_is_unscanned_waits_on_the_threat_scan(store):
    rec = _merge_pick(_rec())
    del rec["threat"]
    out = _classify(store, rec)
    assert (out["column"], out["bucket"]) == ("auto", "waiting")
    assert "threat scan" in out["reason"]


def test_a_merge_pick_red_at_an_earlier_head_waits_on_the_security_hunter(store):
    rec = _merge_pick(_rec())
    rec["security"] = {"verdict": "RED", "checked_at": _now(), "against_head_sha": "b" * 40,
                       "findings": [{"title": "drops an auth check"}]}
    out = _classify(store, rec)
    assert (out["column"], out["bucket"]) == ("auto", "waiting")
    assert "RED at an earlier head" in out["reason"]


def test_an_old_red_at_the_current_head_is_handed_to_you(store):
    rec = _merge_pick(_rec())
    rec["security"] = {"verdict": "RED", "checked_at": "2026-01-02T00:00:00+00:00",
                       "against_head_sha": HEAD, "findings": [{"title": "drops an auth check"}]}
    out = _classify(store, rec)
    assert out["column"] == "handed" and out["owner"] == "you"


def test_a_merge_pick_no_review_reached_waits_on_the_security_hunter(store):
    rec = _merge_pick(_rec())
    del rec["security"]
    out = _classify(store, rec)
    assert (out["column"], out["bucket"]) == ("auto", "waiting")
    assert "security review has not run" in out["reason"]


def _layer(n: int, head_ref: str, base: str, head_repo: str = "test-owner/test-repo") -> dict:
    rec = _rec(n)
    rec["meta"].update(head_ref=head_ref, head_repo=head_repo, base=base)
    rec["security"] = {"verdict": "GREEN", "findings": [], "override": None,
                       "checked_at": _now(), "against_head_sha": HEAD}
    return rec


def test_a_stacked_pr_waits_on_the_pr_it_is_stacked_on(store):
    store.save_pr(_layer(10, "layer-1", "trunk"))
    out = _classify(store, _layer(11, "layer-2", "layer-1"))
    assert (out["column"], out["bucket"], out["owner"]) == ("handed", "stacked", "author")
    assert out["reason"].startswith("stacked on #10 (layer-1)")
    assert service.stacked_on(store.load_pr(10)) is None


def test_a_fork_branch_of_the_same_name_is_not_the_base(store):
    store.save_pr(_layer(10, "layer-1", "trunk", head_repo="someone/fork"))
    out = _classify(store, _layer(11, "layer-2", "layer-1"))
    assert out["bucket"] == "stacked"
    assert out["reason"].startswith("stacked on branch layer-1")
