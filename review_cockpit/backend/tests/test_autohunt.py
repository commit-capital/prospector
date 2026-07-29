"""The verify worker's idle auto-hunt: two-lane deterministic selection —
security review for clean merge candidates lacking a current verdict, then
sandbox verification for GREEN-cleared candidates — pain-ordered, gated by
TRIAGE_VERIFY_AUTOHUNT."""
from __future__ import annotations

import pytest

from pipeline import store as S
from pipeline.storekit import now as _now
from review_cockpit.backend import data
from review_cockpit.backend import service
from review_cockpit.backend import verify_worker

HEAD = "a" * 40


def _clean_merge_pr(n: int, *, pain: float = 0.0) -> dict:
    """A gate-clean merge-disposition PR record (the shape test_gates._pr
    uses) with one explicit linked issue carrying `pain`."""
    now = _now()
    return {
        "pr": n,
        "meta": {"title": f"fix {n}", "author": "dev", "state": "open",
                 "draft": False, "head_sha": HEAD, "checked_at": now},
        "signals": {"greptile": 5, "ci": "passing", "mergeable": True,
                    "has_tests": True, "checked_at": now, "against_head_sha": HEAD},
        "drift": {"state": "applicable", "checked_at": now, "against_head_sha": HEAD},
        "analysis": {"disposition": "merge", "rationale": "r", "checked_at": now,
                     "against_head_sha": HEAD},
        "issues": {"linked": [{"issue": 1000 + n, "pain": pain, "how": "explicit"}],
                   "checked_at": now, "against_head_sha": HEAD},
    }


def _green(store: S.Store, n: int) -> None:
    rec = store.load_pr(n).raw
    rec["security"] = {"verdict": "GREEN", "findings": [], "checked_at": _now(),
                       "against_head_sha": HEAD}
    store.save_pr(rec)


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    verify_worker.security_failed.clear()
    monkeypatch.setattr(verify_worker, "_changed_paths", lambda pr: ["src/app.ts"])
    data.refresh()
    return st


def test_autohunt_enabled_is_an_exact_opt_in(monkeypatch):
    monkeypatch.delenv("TRIAGE_VERIFY_AUTOHUNT", raising=False)
    assert verify_worker.enabled_autohunt() is False
    monkeypatch.setenv("TRIAGE_VERIFY_AUTOHUNT", "yes")
    assert verify_worker.enabled_autohunt() is False
    monkeypatch.setenv("TRIAGE_VERIFY_AUTOHUNT", "1")
    assert verify_worker.enabled_autohunt() is True


def test_security_lane_picks_highest_pain_then_oldest(store):
    for n, pain in ((1, 0.2), (2, 0.9), (3, 0.9)):
        store.save_pr(_clean_merge_pr(n, pain=pain))
    data.refresh()
    assert verify_worker.next_auto() == ("security", 2)


def test_security_lane_skips_failed_this_process(store):
    store.save_pr(_clean_merge_pr(1))
    verify_worker.security_failed.add(1)
    data.refresh()
    assert verify_worker.next_auto() is None


def test_stale_green_goes_back_through_security(store):
    """A GREEN computed against an earlier head is not a clearance: the PR
    re-enters the security lane, never the verify lane."""
    store.save_pr(_clean_merge_pr(1))
    rec = store.load_pr(1).raw
    rec["security"] = {"verdict": "GREEN", "findings": [], "checked_at": _now(),
                       "against_head_sha": "0" * 40}
    store.save_pr(rec)
    data.refresh()
    assert verify_worker.next_auto() == ("security", 1)


def test_verify_lane_requires_current_green(store):
    store.save_pr(_clean_merge_pr(1))
    _green(store, 1)
    data.refresh()
    assert verify_worker.next_auto() == ("verify", 1)


def test_verify_lane_orders_by_pain(store):
    for n, pain in ((1, 0.1), (2, 0.8)):
        store.save_pr(_clean_merge_pr(n, pain=pain))
        _green(store, n)
    data.refresh()
    assert verify_worker.next_auto() == ("verify", 2)


def test_verify_lane_skips_current_verify_record(store):
    store.save_pr(_clean_merge_pr(1))
    _green(store, 1)
    rec = store.load_pr(1).raw
    rec["verify"] = {"outcome": "verified-fix", "signals": {},
                     "against_base_sha": "c" * 40, "checked_at": _now(),
                     "against_head_sha": HEAD}
    store.save_pr(rec)
    data.refresh()
    assert verify_worker.next_auto() is None


def test_verify_lane_waits_on_errored_or_cancelled_request(store):
    """An errored or operator-cancelled request is a deliberate stop: the
    hunter never re-fires it — re-queueing is the operator's move."""
    store.save_pr(_clean_merge_pr(1))
    _green(store, 1)
    for status in ("error", "cancelled"):
        store.edit_pr(1).record_verify_request(status, queued_at=_now())
        data.refresh()
        assert verify_worker.next_auto() is None
    store.edit_pr(1).record_verify_request("done", queued_at=_now())
    data.refresh()
    assert verify_worker.next_auto() == ("verify", 1)


def test_verify_lane_fails_closed_on_missing_diff(store, monkeypatch):
    store.save_pr(_clean_merge_pr(1))
    _green(store, 1)
    monkeypatch.setattr(verify_worker, "_changed_paths", lambda pr: [])
    data.refresh()
    assert verify_worker.next_auto() is None


def test_auto_queue_writes_source_auto_and_feeds_the_queue(store):
    store.save_pr(_clean_merge_pr(1))
    _green(store, 1)
    data.refresh()
    verify_worker.auto_queue_verify(1)
    req = store.load_pr(1).verify_request
    assert req["status"] == "queued"
    assert req["source"] == "auto"
    assert verify_worker.next_queued() == 1


def test_run_security_argv_and_failure_memory(store, monkeypatch):
    store.save_pr(_clean_merge_pr(1))
    data.refresh()
    argv_seen: list[list[str]] = []

    class FakeProc:
        stdout = iter(["lens output\n"])

        def wait(self):
            return 1

    def fake_popen(argv, **kw):
        argv_seen.append([str(a) for a in argv])
        return FakeProc()

    monkeypatch.setattr(verify_worker.subprocess, "Popen", fake_popen)
    assert verify_worker.run_security(1) == 1
    assert any("security_review.py" in a for a in argv_seen[0])
    assert "--pr" in argv_seen[0]
    assert "--trigger" in argv_seen[0]
    assert "autohunt" in argv_seen[0]
    assert 1 in verify_worker.security_failed
    assert verify_worker.next_auto() is None


def test_beat_publishes_autohunt_flag_and_failures(store, monkeypatch):
    monkeypatch.setenv("TRIAGE_VERIFY_AUTOHUNT", "1")
    verify_worker.security_failed.add(7)
    verify_worker.beat()
    import socket
    rec = store.load_verify_worker()["hosts"][socket.gethostname()]
    assert rec["autohunt"] is True
    assert rec["security_failed"] == [7]


def test_run_security_rc0_without_verdict_lands_in_failure_memory(store, monkeypatch):
    """security_review.py can exit 0 while holding an INCOMPLETE review (a
    lens agent failure) without writing a verdict. A bare rc==0 check would
    let next_auto() re-pick the same PR immediately, forever — a zero-backoff
    livelock. The presumed-failed pattern (add before spawn, clear only once
    both rc==0 and the PR left the security pool) closes it."""
    store.save_pr(_clean_merge_pr(1))
    data.refresh()

    class FakeProc:
        stdout = iter(["lens output\n"])

        def wait(self):
            return 0

    monkeypatch.setattr(verify_worker.subprocess, "Popen", lambda argv, **kw: FakeProc())
    assert verify_worker.run_security(1) == 0
    assert 1 in verify_worker.security_failed
    data.refresh()
    assert verify_worker.next_auto() is None


def test_run_security_rc0_with_verdict_clears_failure_memory(store, monkeypatch):
    """rc==0 AND a GREEN verdict landed during the run: the PR left the
    security pool for real, so it comes out of security_failed."""
    store.save_pr(_clean_merge_pr(1))
    data.refresh()

    class FakeProc:
        stdout = iter(["lens output\n"])

        def wait(self):
            _green(store, 1)
            return 0

    monkeypatch.setattr(verify_worker.subprocess, "Popen", lambda argv, **kw: FakeProc())
    assert verify_worker.run_security(1) == 0
    assert 1 not in verify_worker.security_failed


def test_run_security_pre_spawn_recheck_skips_already_cleared(store, monkeypatch):
    """A fresh pre-spawn recheck: if another process already delivered a
    verdict since this PR was picked (snapshot lag), run_security returns
    without spawning at all."""
    store.save_pr(_clean_merge_pr(1))
    _green(store, 1)
    data.refresh()
    spawned = {"count": 0}

    def fake_popen(argv, **kw):
        spawned["count"] += 1
        raise AssertionError("must not spawn a subprocess")

    monkeypatch.setattr(verify_worker.subprocess, "Popen", fake_popen)
    assert verify_worker.run_security(1) == 0
    assert spawned["count"] == 0


def test_next_queued_ranks_operator_ahead_of_auto(store):
    """An operator click never waits behind an earlier auto-queued request:
    next_queued orders operator picks before auto picks regardless of age."""
    store.save_pr(_clean_merge_pr(1))
    store.save_pr(_clean_merge_pr(2))
    store.edit_pr(1).record_verify_request(
        "queued", queued_at="2026-07-01T00:00:00+00:00", source="auto")
    store.edit_pr(2).record_verify_request(
        "queued", queued_at="2026-07-20T00:00:00+00:00", source=None)
    data.refresh()
    assert verify_worker.next_queued() == 2


def test_next_auto_prefers_security_lane_over_verify_lane(store):
    """PR 1 lacks a security verdict; PR 2 is GREEN-cleared and
    verify-eligible. The security lane wins as long as any PR needs review."""
    store.save_pr(_clean_merge_pr(1))
    store.save_pr(_clean_merge_pr(2))
    _green(store, 2)
    data.refresh()
    assert verify_worker.next_auto() == ("security", 1)


def test_status_counts_pools_and_reads_registry(store):
    from review_cockpit.backend import autohunt_view
    store.save_pr(_clean_merge_pr(1))          # needs security
    store.save_pr(_clean_merge_pr(2))
    _green(store, 2)                           # GREEN -> verify pool
    store.save_pr(_clean_merge_pr(3))          # needs security, but parked
    store.save_verify_worker({"host": "studio", "pid": 1, "last_beat": _now(),
                              "current_pr": None, "autohunt": True,
                              "security_failed": [9, 3]})
    data.refresh()
    s = autohunt_view.status()
    assert s["enabled"] is True
    # PR 3 also lacks a security verdict, but it is parked in security_failed
    # (not awaiting pickup — renders as a failed chip instead), so it is
    # excluded from the pool count.
    assert s["security_pool"] == 1
    assert s["verify_pool"] == 1
    assert s["security_failed"] == [3, 9]
    assert s["runner"]["host"] == "studio"


def test_status_defaults_when_registry_empty(store):
    from review_cockpit.backend import autohunt_view
    data.refresh()
    s = autohunt_view.status()
    assert s["enabled"] is False
    assert s["security_failed"] == []
    assert s["verify_failed"] == []


def test_status_collects_auto_verify_failures(store):
    """Only auto-queued requests that ended in error land in verify_failed:
    an operator-queued error stays off the panel (it is the operator's own
    run), and a live auto request is still awaiting pickup, not failed."""
    from review_cockpit.backend import autohunt_view
    for n in (1, 2, 3, 4):
        store.save_pr(_clean_merge_pr(n))
    store.edit_pr(1).record_verify_request(
        "error", queued_at=_now(), error_kind="interrupted", source="auto")
    store.edit_pr(2).record_verify_request(
        "error", queued_at=_now(), error_kind="no-base")
    store.edit_pr(3).record_verify_request(
        "queued", queued_at=_now(), source="auto")
    store.edit_pr(4).record_verify_request(
        "error", queued_at=_now(), source="auto")
    data.refresh()
    s = autohunt_view.status()
    assert s["verify_failed"] == [{"pr": 1, "error_kind": "interrupted"},
                                  {"pr": 4, "error_kind": None}]


def test_history_normalizes_newest_first(store):
    from review_cockpit.backend import autohunt_view
    store.save_pr(_clean_merge_pr(1))
    data.refresh()
    store.append_run({"phase": "ingest", "stats": {}})
    store.append_run({"phase": "security:review-one", "pr": 1,
                      "started": "2026-07-20T01:00:00+00:00",
                      "finished": "2026-07-20T01:05:00+00:00",
                      "trigger": "autohunt", "stats": {"verdict": "GREEN"}})
    store.append_run({"phase": "verify:single", "pr": 1,
                      "started": "2026-07-20T02:00:00+00:00",
                      "finished": "2026-07-20T02:20:00+00:00",
                      "stats": {"status": "done", "error_kind": None,
                                "outcome": "verified-fix"}})
    rows = autohunt_view.history()
    assert [r["phase"] for r in rows] == ["verify", "security"]
    assert rows[0]["result"] == "verified-fix"
    assert rows[0]["trigger"] is None
    assert rows[1]["result"] == "GREEN"
    assert rows[1]["trigger"] == "autohunt"
    assert rows[1]["title"] == "fix 1"


def test_history_result_names_the_error_kind(store):
    from review_cockpit.backend import autohunt_view
    store.append_run({"phase": "verify:single", "pr": 3,
                      "stats": {"status": "error", "error_kind": "no-base",
                                "outcome": None}})
    rows = autohunt_view.history()
    assert rows[0]["result"] == "error:no-base"
    assert rows[0]["title"] is None


def test_service_changed_paths_empty_when_no_diff_cached(store):
    store.save_pr(_clean_merge_pr(1))
    data.refresh()
    pr = data.prs()[1]
    assert service.changed_paths(pr) == []


def test_history_respects_limit(store):
    from review_cockpit.backend import autohunt_view
    for i in range(5):
        store.append_run({"phase": "security:review-one", "pr": i,
                          "stats": {"verdict": "GREEN"}})
    assert len(autohunt_view.history(limit=3)) == 3


def test_history_window_filters_by_days(store, monkeypatch):
    """A run older than the requested window is excluded; one inside it is
    kept — the day-window scan, unlike history()'s bounded tail, finds a
    matching run no matter how far back it sits in the ledger."""
    from review_cockpit.backend import autohunt_view
    old = "2020-01-01T00:00:00+00:00"
    recent = _now()
    store.append_run({"phase": "security:review-one", "pr": 1, "ts": old,
                      "started": old, "finished": old, "stats": {"verdict": "RED"}})
    store.append_run({"phase": "security:review-one", "pr": 2, "ts": recent,
                      "started": recent, "finished": recent, "stats": {"verdict": "GREEN"}})
    rows = autohunt_view.history_window(days=7)
    assert [r["pr"] for r in rows] == [2]


def test_history_window_none_is_all_time(store):
    from review_cockpit.backend import autohunt_view
    old = "2020-01-01T00:00:00+00:00"
    store.append_run({"phase": "security:review-one", "pr": 1, "ts": old,
                      "stats": {"verdict": "RED"}})
    rows = autohunt_view.history_window(days=None)
    assert [r["pr"] for r in rows] == [1]


def test_history_window_caps_at_hard_limit(store, monkeypatch):
    from review_cockpit.backend import autohunt_view
    monkeypatch.setattr(autohunt_view, "HISTORY_LIMIT_CAP", 2)
    for i in range(5):
        store.append_run({"phase": "verify:single", "pr": i,
                          "stats": {"outcome": "verified-fix"}})
    assert len(autohunt_view.history_window(days=None, limit=100)) == 2


def test_summary_counts_totals_and_results_within_window(store):
    from review_cockpit.backend import autohunt_view
    old = "2020-01-01T00:00:00+00:00"
    recent = _now()
    store.append_run({"phase": "security:review-one", "pr": 1, "ts": old,
                      "stats": {"verdict": "RED"}})
    store.append_run({"phase": "security:review-one", "pr": 2, "ts": recent,
                      "stats": {"verdict": "GREEN"}})
    store.append_run({"phase": "security:review-one", "pr": 3, "ts": recent,
                      "stats": {"verdict": "GREEN"}})
    store.append_run({"phase": "verify:single", "pr": 2, "ts": recent,
                      "stats": {"outcome": "verified-fix"}})
    sum7 = autohunt_view.summary(days=7)
    assert sum7["days"] == 7
    assert sum7["security"] == {"total": 2, "by_result": {"GREEN": 2},
                                "pr_ids_by_result": {"GREEN": [2, 3]}}
    assert sum7["verify"] == {"total": 1, "by_result": {"verified-fix": 1},
                              "pr_ids_by_result": {"verified-fix": [2]}}
    sum_all = autohunt_view.summary(days=None)
    assert sum_all["security"] == {"total": 3, "by_result": {"GREEN": 2, "RED": 1},
                                   "pr_ids_by_result": {"GREEN": [2, 3], "RED": [1]}}


def test_summary_dedupes_pr_ids_across_repeated_runs(store):
    """The same PR re-run twice with the same result contributes one PR number
    to that bucket, not two — the chip's click target is "which PRs", not "how
    many runs"."""
    from review_cockpit.backend import autohunt_view
    recent = _now()
    store.append_run({"phase": "security:review-one", "pr": 5, "ts": recent,
                      "stats": {"verdict": "GREEN"}})
    store.append_run({"phase": "security:review-one", "pr": 5, "ts": recent,
                      "stats": {"verdict": "GREEN"}})
    s = autohunt_view.summary(days=None)
    assert s["security"]["by_result"]["GREEN"] == 2
    assert s["security"]["pr_ids_by_result"]["GREEN"] == [5]


def test_summary_ignores_non_hunt_phases(store):
    from review_cockpit.backend import autohunt_view
    store.append_run({"phase": "ingest", "ts": _now(), "stats": {}})
    s = autohunt_view.summary(days=None)
    assert s["security"]["total"] == 0
    assert s["verify"]["total"] == 0
