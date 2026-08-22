"""The verify worker's idle auto-hunt: two-lane deterministic selection —
security review for clean merge candidates lacking a current verdict, then
sandbox verification for GREEN-cleared candidates — pain-ordered, gated by
TRIAGE_VERIFY_AUTOHUNT."""
from __future__ import annotations

import pytest

from pipeline import store as S
from pipeline.storekit import now as _now
from prospector_app.backend import data
from prospector_app.backend import service
from prospector_app.backend import verify_worker
from pipeline.testsupport import reviews_section

HEAD = "a" * 40


def _iso_hours_ago(hours: float) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()



def _clean_merge_pr(n: int, *, pain: float = 0.0) -> dict:
    """A gate-clean merge-disposition PR record (the shape test_gates._pr
    uses) with one explicit linked issue carrying `pain`."""
    now = _now()
    return {
        "pr": n,
        "meta": {"title": f"fix {n}", "author": "dev", "state": "open",
                 "draft": False, "head_sha": HEAD, "checked_at": now},
        "signals": {"ci": "passing", "mergeable": True,
                    "has_tests": True, "checked_at": now, "against_head_sha": HEAD},
        "reviews": reviews_section(HEAD, now),
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
    assert verify_worker.run_security(1) is None
    assert spawned["count"] == 0


def test_run_security_declines_a_pr_another_machine_claimed(store, monkeypatch):
    """Two idle hunters must not both spend a multi-agent review on one PR. The
    claim is the store's, so losing it means dropping the pick."""
    store.save_pr(_clean_merge_pr(1))
    data.refresh()
    assert store.claim_security_run(
        1, host="some-other-machine",
        stale_after=verify_worker.SECURITY_CLAIM_SECONDS) is True

    def fake_popen(argv, **kw):
        raise AssertionError("must not spawn a subprocess")

    monkeypatch.setattr(verify_worker.subprocess, "Popen", fake_popen)
    assert verify_worker.run_security(1) is None


def test_run_security_releases_its_claim_when_the_run_fails(store, monkeypatch):
    """A failed review leaves the PR free for the next attempt — a claim that
    outlived its run would park the PR until the window expired."""
    store.save_pr(_clean_merge_pr(1))
    data.refresh()

    def boom(argv, **kw):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(verify_worker.subprocess, "Popen", boom)
    with pytest.raises(RuntimeError):
        verify_worker.run_security(1)
    assert store.claim_security_run(
        1, host="some-other-machine",
        stale_after=verify_worker.SECURITY_CLAIM_SECONDS) is True


def test_release_stale_claims_frees_this_hosts_leftovers(store):
    """A review does not survive its process, so a claim in this host's name
    after a restart is a leftover."""
    import socket
    store.save_pr(_clean_merge_pr(1))
    store.save_pr(_clean_merge_pr(2))
    store.claim_security_run(1, host=socket.gethostname(), stale_after=3600)
    store.claim_security_run(2, host="some-other-machine", stale_after=3600)
    data.refresh()

    assert verify_worker.release_stale_claims() == [1]
    assert store.claim_security_run(2, host="a-third-machine", stale_after=3600) is False


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
    from prospector_app.backend import autohunt_view
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
    from prospector_app.backend import autohunt_view
    data.refresh()
    s = autohunt_view.status()
    assert s["enabled"] is False
    assert s["security_failed"] == []
    assert s["verify_failed"] == []


def test_status_collects_all_verify_failures(store):
    """Every errored verify request lands in verify_failed, auto-queued or
    operator-queued alike, each tagged with its `source`; a live auto request
    is still awaiting pickup, not failed."""
    from prospector_app.backend import autohunt_view
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
    assert s["verify_failed"] == [{"pr": 1, "error_kind": "interrupted", "source": "auto"},
                                  {"pr": 2, "error_kind": "no-base", "source": None},
                                  {"pr": 4, "error_kind": None, "source": "auto"}]


def test_history_normalizes_newest_first(store):
    from prospector_app.backend import autohunt_view
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
    from prospector_app.backend import autohunt_view
    store.append_run({"phase": "verify:single", "pr": 3,
                      "stats": {"status": "error", "error_kind": "no-base",
                                "outcome": None}})
    rows = autohunt_view.history()
    assert rows[0]["result"] == "error:no-base"
    assert rows[0]["title"] is None


def test_history_result_is_the_run_own_state_not_a_carried_outcome(store):
    """A run that ended anywhere but `done` reports that state. The ledger's
    `outcome` stat is the PR's stored outcome read after the run, so a park or
    a failure carries whatever an earlier run left behind — rendering it would
    show a no-base park as a verification that never happened."""
    from prospector_app.backend import autohunt_view
    store.append_run({"phase": "verify:single", "pr": 1,
                      "stats": {"status": "waiting-for-base", "error_kind": "no-base",
                                "outcome": "agent-verified"}})
    store.append_run({"phase": "verify:single", "pr": 2,
                      "stats": {"status": "error", "error_kind": "sandbox-error",
                                "outcome": "verified-fix"}})
    store.append_run({"phase": "verify:single", "pr": 3,
                      "stats": {"status": "cancelled", "error_kind": None,
                                "outcome": "escalate"}})
    rows = autohunt_view.history()
    assert [r["result"] for r in rows] == ["cancelled", "error:sandbox-error",
                                           "waiting-for-base"]


def test_history_result_keeps_the_outcome_of_a_completed_run(store):
    """`done` is the one status whose outcome this run committed, so it is the
    result shown; a done run that recorded none falls back to the status."""
    from prospector_app.backend import autohunt_view
    store.append_run({"phase": "verify:single", "pr": 1,
                      "stats": {"status": "done", "error_kind": None,
                                "outcome": "unverifiable-no-test"}})
    store.append_run({"phase": "verify:single", "pr": 2,
                      "stats": {"status": "done", "error_kind": None, "outcome": None}})
    rows = autohunt_view.history()
    assert [r["result"] for r in rows] == ["done", "unverifiable-no-test"]


def test_summary_counts_a_park_under_its_own_state(store):
    """summary() buckets by the same result word the history table shows, so a
    no-base park never inflates the verified counts."""
    from prospector_app.backend import autohunt_view
    store.append_run({"phase": "verify:single", "pr": 1,
                      "stats": {"status": "waiting-for-base", "error_kind": "no-base",
                                "outcome": "agent-verified"}})
    store.append_run({"phase": "verify:single", "pr": 2,
                      "stats": {"status": "done", "error_kind": None,
                                "outcome": "agent-verified"}})
    verify = autohunt_view.summary(days=None)["verify"]
    assert verify["by_result"] == {"waiting-for-base": 1, "agent-verified": 1}
    assert verify["pr_ids_by_result"] == {"waiting-for-base": [1], "agent-verified": [2]}


def test_service_changed_paths_empty_when_no_diff_cached(store):
    store.save_pr(_clean_merge_pr(1))
    data.refresh()
    pr = data.prs()[1]
    assert service.changed_paths(pr) == []


def test_history_respects_limit(store):
    from prospector_app.backend import autohunt_view
    for i in range(5):
        store.append_run({"phase": "security:review-one", "pr": i,
                          "stats": {"verdict": "GREEN"}})
    assert len(autohunt_view.history(limit=3)) == 3


def test_history_window_filters_by_days(store, monkeypatch):
    """A run older than the requested window is excluded; one inside it is
    kept — the day-window scan, unlike history()'s bounded tail, finds a
    matching run no matter how far back it sits in the ledger."""
    from prospector_app.backend import autohunt_view
    old = "2020-01-01T00:00:00+00:00"
    recent = _now()
    store.append_run({"phase": "security:review-one", "pr": 1, "ts": old,
                      "started": old, "finished": old, "stats": {"verdict": "RED"}})
    store.append_run({"phase": "security:review-one", "pr": 2, "ts": recent,
                      "started": recent, "finished": recent, "stats": {"verdict": "GREEN"}})
    rows = autohunt_view.history_window(days=7)
    assert [r["pr"] for r in rows] == [2]


def test_history_window_none_is_all_time(store):
    from prospector_app.backend import autohunt_view
    old = "2020-01-01T00:00:00+00:00"
    store.append_run({"phase": "security:review-one", "pr": 1, "ts": old,
                      "stats": {"verdict": "RED"}})
    rows = autohunt_view.history_window(days=None)
    assert [r["pr"] for r in rows] == [1]


def test_history_window_caps_at_hard_limit(store, monkeypatch):
    from prospector_app.backend import autohunt_view
    monkeypatch.setattr(autohunt_view, "HISTORY_LIMIT_CAP", 2)
    for i in range(5):
        store.append_run({"phase": "verify:single", "pr": i,
                          "stats": {"outcome": "verified-fix"}})
    assert len(autohunt_view.history_window(days=None, limit=100)) == 2


def test_history_window_lanes_filters_to_one_lane(store):
    """`lanes` restricts the ledger scan to just that lane — the Verify Queue
    panel's history is verify-only, unlike the combined Auto-hunt table."""
    from prospector_app.backend import autohunt_view
    store.append_run({"phase": "security:review-one", "pr": 1,
                      "stats": {"verdict": "GREEN"}})
    store.append_run({"phase": "verify:single", "pr": 2,
                      "stats": {"outcome": "verified-fix"}})
    rows = autohunt_view.history_window(days=None, lanes=frozenset({"verify"}))
    assert [r["phase"] for r in rows] == ["verify"]
    assert rows[0]["pr"] == 2


def test_summary_counts_totals_and_results_within_window(store):
    from prospector_app.backend import autohunt_view
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
    from prospector_app.backend import autohunt_view
    recent = _now()
    store.append_run({"phase": "security:review-one", "pr": 5, "ts": recent,
                      "stats": {"verdict": "GREEN"}})
    store.append_run({"phase": "security:review-one", "pr": 5, "ts": recent,
                      "stats": {"verdict": "GREEN"}})
    s = autohunt_view.summary(days=None)
    assert s["security"]["by_result"]["GREEN"] == 2
    assert s["security"]["pr_ids_by_result"]["GREEN"] == [5]


def test_summary_ignores_non_hunt_phases(store):
    from prospector_app.backend import autohunt_view
    store.append_run({"phase": "ingest", "ts": _now(), "stats": {}})
    s = autohunt_view.summary(days=None)
    assert s["security"]["total"] == 0
    assert s["verify"]["total"] == 0


class TestBaseHealth:
    """Each verification machine's pinned base, surfaced so a verify lane that
    has silently stopped tracking master is visible rather than inferred from a
    queue full of parked requests."""

    def _pin(self, store, host: str = "mac-studio", **extra: object) -> None:
        store.save_verify_base({"host": host, "base_sha": "a" * 40, "tier": 1,
                                "pinned_at": _now(), "baseline_failing": [],
                                "baseline_captured_at": _now(), **extra})

    def test_a_fresh_pin_is_healthy(self, store):
        from prospector_app.backend import autohunt_view
        self._pin(store)
        base = autohunt_view.status()["base"]["hosts"][0]
        assert base["base_sha"] == "a" * 12
        assert base["tier"] == 1
        assert base["stale"] is False
        assert base["refresh_failures"] == 0

    def test_a_pin_one_day_old_is_not_yet_stale(self, store):
        """One missed daily refresh is a hiccup, not a broken lane."""
        from prospector_app.backend import autohunt_view
        self._pin(store, pinned_at=_iso_hours_ago(30))
        assert autohunt_view.status()["base"]["hosts"][0]["stale"] is False

    def test_a_pin_well_past_the_refresh_window_is_stale(self, store):
        from prospector_app.backend import autohunt_view
        self._pin(store, pinned_at=_iso_hours_ago(72))
        base = autohunt_view.status()["base"]["hosts"][0]
        assert base["stale"] is True
        assert base["age_hours"] == pytest.approx(72, abs=1)

    def test_a_failing_refresh_carries_its_error_and_run_length(self, store):
        from prospector_app.backend import autohunt_view
        self._pin(store, refresh_ok=False, refresh_failures=3,
                  refresh_error="CalledProcessError: no space left on device")
        base = autohunt_view.status()["base"]["hosts"][0]
        assert base["refresh_failures"] == 3
        assert base["refresh_ok"] is False
        assert "no space left" in base["refresh_error"]

    def test_a_store_no_machine_has_pinned_reports_no_base(self, store):
        from prospector_app.backend import autohunt_view
        assert autohunt_view.status()["base"]["hosts"] == []

    def test_each_machine_reports_its_own_pin(self, store):
        """Two machines drift a few hours apart by design, so a lagging one is
        visible on its own row."""
        from prospector_app.backend import autohunt_view
        self._pin(store, host="mac-studio")
        self._pin(store, host="devin-mbp", pinned_at=_iso_hours_ago(72))
        hosts = autohunt_view.status()["base"]["hosts"]
        assert [h["host"] for h in hosts] == ["devin-mbp", "mac-studio"]
        assert [h["stale"] for h in hosts] == [True, False]

    def test_an_unparseable_pin_timestamp_does_not_read_as_stale(self, store):
        """A malformed stamp is not evidence the lane is broken, and a false
        alarm on the Control tab trains the operator to ignore it."""
        from prospector_app.backend import autohunt_view
        self._pin(store, pinned_at="not-a-timestamp")
        base = autohunt_view.status()["base"]["hosts"][0]
        assert base["age_hours"] is None
        assert base["stale"] is False


# ---------------------------------------------------------------------------
# The repro re-sweep lane: a concluded verification whose independent repro the
# harness itself broke gets exactly one more sandbox run per head.

def _verified(store: S.Store, n: int, *, signals: dict, findings=()) -> None:
    rec = store.load_pr(n).raw
    rec["verify"] = {"outcome": "verified-fix", "tier": 1, "signals": signals,
                     "findings": list(findings), "against_base_sha": "b" * 40,
                     "against_head_sha": HEAD, "checked_at": _now()}
    store.save_pr(rec)


_BROKEN_SIGNALS: dict = {
    "blind_adequacy": {"repro_command": "npx vitest run --config server/v.ts x"},
    "independent_repro": {"ran": True, "exit_code": 20},
    "repro_reason_match": {"matches": False, "applicable": True,
                           "confidence": "high"}}
_MISROOTED = ({"signal": "misrooted-repro-config", "note": "unrunnable"},)


def _broken_repro_pr(store: S.Store, n: int, *, pain: float = 0.0,
                     findings=_MISROOTED) -> None:
    """A GREEN-cleared PR whose verification concluded verified-fix but whose
    independent repro the harness broke."""
    store.save_pr(_clean_merge_pr(n, pain=pain))
    _green(store, n)
    _verified(store, n, signals=_BROKEN_SIGNALS, findings=findings)
    data.refresh()


def test_resweep_lane_picks_a_harness_broken_repro(store):
    _broken_repro_pr(store, 1)
    assert verify_worker.next_auto() == ("resweep", 1)


def test_resweep_lane_orders_by_pain(store):
    _broken_repro_pr(store, 1, pain=0.2)
    _broken_repro_pr(store, 2, pain=0.9)
    assert verify_worker.next_auto() == ("resweep", 2)


def test_a_corroborated_verification_is_never_reswept(store):
    store.save_pr(_clean_merge_pr(1))
    _green(store, 1)
    _verified(store, 1, signals={
        "blind_adequacy": {"repro_command": "npx vitest run x"},
        "independent_repro": {"ran": True, "exit_code": 20},
        "repro_reason_match": {"matches": True, "applicable": True,
                               "confidence": "high"}})
    data.refresh()
    assert verify_worker.next_auto() is None


def test_a_repro_that_ran_correctly_and_did_not_reproduce_is_not_reswept(store):
    """No harness signal: the command ran as written and the defect did not
    show. That is evidence about the PR, and re-running only repeats it."""
    _broken_repro_pr(store, 1, findings=())
    assert verify_worker.next_auto() is None


def test_a_never_authored_repro_is_not_reswept(store):
    """Nothing was attempted, so there is no harness defect to clear — a re-run
    re-derives the same null."""
    store.save_pr(_clean_merge_pr(1))
    _green(store, 1)
    _verified(store, 1, signals={"blind_adequacy": {"repro_command": None}})
    data.refresh()
    assert verify_worker.next_auto() is None


def test_an_authored_repro_that_never_ran_is_reswept(store):
    store.save_pr(_clean_merge_pr(1))
    _green(store, 1)
    _verified(store, 1, signals={
        "blind_adequacy": {"repro_command": "npx vitest run x"},
        "independent_repro": {"ran": False, "exit_code": None}})
    data.refresh()
    assert verify_worker.next_auto() == ("resweep", 1)


def test_a_repro_skipped_for_targeting_a_pr_test_is_not_reswept(store):
    """A deliberate pre-run skip is a decision about the command's target, not
    a harness defect — the retry already happened inside the blind lane."""
    store.save_pr(_clean_merge_pr(1))
    _green(store, 1)
    _verified(store, 1, signals={
        "blind_adequacy": {"repro_command": "npx vitest run x"},
        "independent_repro": {"ran": False, "exit_code": None,
                              "skipped_reason": "repro-targets-pr-test"}})
    data.refresh()
    assert verify_worker.next_auto() is None


def test_the_resweep_is_spent_once_its_own_record_lands(store):
    """The loop guard: after a re-sweep writes a record that is STILL broken,
    the PR never queues itself again."""
    _broken_repro_pr(store, 1)
    assert verify_worker.next_auto() == ("resweep", 1)
    store.load_pr(1).record_verify_request(
        "done", queued_at=_now(), source=verify_worker.RESWEEP_SOURCE)
    # The re-verified record lands after the re-sweep request was queued.
    _verified(store, 1, signals=_BROKEN_SIGNALS, findings=_MISROOTED)
    data.refresh()
    assert verify_worker.next_auto() is None


def test_the_retired_finding_spelling_still_resweeps(store):
    """Records written before the finding was renamed carry the old signal for
    the same unrunnable command, and must not be stranded by the rename."""
    _broken_repro_pr(store, 1, findings=(
        {"signal": "vacuous-repro-filter", "note": "unrunnable"},))
    assert verify_worker.next_auto() == ("resweep", 1)


def test_the_new_name_filter_finding_resweeps(store):
    _broken_repro_pr(store, 1, findings=(
        {"signal": "vacuous-repro-name-filter", "note": "matched no title"},))
    assert verify_worker.next_auto() == ("resweep", 1)


def test_an_in_flight_request_blocks_the_resweep(store):
    _broken_repro_pr(store, 1)
    store.load_pr(1).record_verify_request("queued", queued_at=_now())
    data.refresh()
    assert verify_worker.next_auto() is None


def test_the_verify_lane_outranks_the_resweep_lane(store):
    """An unverified candidate buys more than a second opinion on a concluded
    one, however painful the concluded one is."""
    _broken_repro_pr(store, 1, pain=0.9)
    store.save_pr(_clean_merge_pr(2, pain=0.1))
    _green(store, 2)
    data.refresh()
    assert verify_worker.next_auto() == ("verify", 2)


class TestSecurityClaimedElsewhere:
    """Another machine's live claim on the top security pick. The hunter must
    neither spin on it nor spend a second review: the pool skips a PR under
    review elsewhere, and the drain loop rests after a lost claim."""

    def test_pool_skips_a_pr_another_host_holds(self, store, monkeypatch):
        store.save_pr(_clean_merge_pr(1, pain=0.9))
        store.save_pr(_clean_merge_pr(2, pain=0.1))
        monkeypatch.setattr(verify_worker.socket, "gethostname", lambda: "mac")
        assert store.claim_security_run(1, host="linux", stale_after=3600) is True
        data.refresh()
        assert verify_worker.next_auto() == ("security", 2)

    def test_pool_keeps_this_hosts_own_leftover_claim(self, store, monkeypatch):
        store.save_pr(_clean_merge_pr(1))
        monkeypatch.setattr(verify_worker.socket, "gethostname", lambda: "mac")
        assert store.claim_security_run(1, host="mac", stale_after=3600) is True
        data.refresh()
        assert verify_worker.next_auto() == ("security", 1)

    def test_pool_keeps_a_pr_whose_claim_went_stale(self, store, monkeypatch):
        store.save_pr(_clean_merge_pr(1))
        monkeypatch.setattr(verify_worker.socket, "gethostname", lambda: "mac")
        rec = store.load_pr(1).raw
        rec["security_run"] = {"host": "linux", "started_at": _iso_hours_ago(5)}
        store.save_pr(rec)
        data.refresh()
        assert verify_worker.next_auto() == ("security", 1)

    def test_drain_loop_rests_after_a_lost_claim(self, store, monkeypatch):
        """The snapshot that picked the PR is behind the store that refused the
        claim: the loop refreshes and rests a poll instead of re-picking the
        same PR back to back."""
        store.save_pr(_clean_merge_pr(1))
        data.refresh()
        monkeypatch.setattr(verify_worker.socket, "gethostname", lambda: "mac")
        monkeypatch.setenv("TRIAGE_VERIFY_AUTOHUNT", "1")
        monkeypatch.setattr(verify_worker, "maybe_refresh_base", lambda: None)
        monkeypatch.setattr(verify_worker, "next_queued", lambda: None)
        attempts: list[int] = []
        real_run_security = verify_worker.run_security

        def run_security(n: int) -> int | None:
            attempts.append(n)
            # Another machine wins the claim between the pick and the attempt.
            store.claim_security_run(n, host="linux", stale_after=3600)
            return real_run_security(n)
        monkeypatch.setattr(verify_worker, "run_security", run_security)

        waits: list[float] = []

        class StopAfterOneWait:
            """Set by the first wait; a loop that never waits is cut off after
            a few ticks so a regression fails instead of hanging."""
            def __init__(self) -> None:
                self._set = False
                self._ticks = 0

            def is_set(self) -> bool:
                self._ticks += 1
                return self._set or self._ticks > 5

            def wait(self, seconds: float) -> bool:
                waits.append(seconds)
                self._set = True
                return True
        monkeypatch.setattr(verify_worker, "stop", StopAfterOneWait())
        verify_worker._drain_loop()
        assert attempts == [1]
        assert waits == [verify_worker.POLL_SECONDS]
