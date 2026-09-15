"""automation.classify: one standing per open PR, in the columns Home shows."""
from __future__ import annotations

import pytest

from pipeline import store as S
from pipeline.storekit import now as _now
from pipeline.testsupport import greptile_entry, reviews_section
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


def test_red_ci_at_the_author_s_head_is_the_author_s(store):
    out = _classify(store, _rec(ci="failing", greptile=4))
    assert (out["column"], out["bucket"], out["owner"]) == ("handed", "author-ci", "author")


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


def test_a_merge_pick_whose_verification_hit_a_machine_fault_is_in_motion(store):
    rec = _merge_pick(_rec(), verify_request={
        "status": "error", "error_kind": "base-lane", "queued_at": _now(),
        "finished_at": _now(), "host": "elsewhere", "against_head_sha": HEAD,
        "error": "The repository's own build command fails on master itself"})
    out = _classify(store, rec)
    assert out["column"] != "act"
    assert out["bucket"] in ("waiting", "other")
    assert "verification" in out["reason"]
