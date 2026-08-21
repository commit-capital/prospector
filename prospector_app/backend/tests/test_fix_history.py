"""What an autofix run leaves behind after it ends.

A fix action's outcome outlives the request that produced it: the runs ledger
keeps it after the next queue click overwrites the PR's fix_request, and the
queue view keeps it visible for long enough that a run finishing between two
polls is still readable when an operator looks.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline import store as S
from prospector_app.backend import autohunt_view, data, fix_history_backfill, fix_queue, fix_worker

HEAD = "a" * 40
NOW = "2026-06-10T00:00:00+00:00"


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    st.save_pr({"pr": 1, "meta": {"title": "fix boom", "state": "open",
                                  "head_sha": HEAD}})
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setattr(fix_worker, "_resubmit",
                        lambda n, *a, stdin=None: type("R", (), {
                            "returncode": 0, "stdout": "", "stderr": ""})())
    data.refresh()
    return st


def _fix_runs(store: S.Store) -> list[dict]:
    return [r.raw for r in store.runs() if getattr(r, "phase", None) == "fix:single"]


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


# --- the ledger entry every ending writes ---------------------------------------

def test_a_refusal_records_its_reason_in_the_ledger(store):
    fix_worker._refuse(1, {"action": "rebase", "source": "auto",
                           "started_at": NOW, "against_head_sha": HEAD},
                       "the base conflicts in server/src/index.ts")

    run, = _fix_runs(store)
    assert run["pr"] == 1
    assert run["started"] == NOW
    assert run["finished"] is not None
    assert run["trigger"] == "autohunt"
    assert run["stats"]["status"] == "refused"
    assert run["stats"]["action"] == "rebase"
    assert run["stats"]["detail"] == "the base conflicts in server/src/index.ts"


def test_a_failure_records_its_error(store):
    fix_worker._fail(1, {"action": "update", "started_at": NOW}, "resubmit exploded")

    run, = _fix_runs(store)
    assert run["stats"]["status"] == "failed"
    assert run["stats"]["detail"] == "resubmit exploded"
    assert run["trigger"] is None, "an operator-queued run is not an autohunt run"


def test_parking_for_review_is_a_finished_run(store):
    fix_worker._park(1, {"action": "rebase", "started_at": NOW}, "rebase",
                     {"patch": "diff"}, "somehost")

    run, = _fix_runs(store)
    assert run["stats"]["status"] == "awaiting-review"
    assert run["stats"]["host"] == "somehost"


def test_a_push_is_its_own_run(store):
    fix_worker._finish_pushed(1, {"action": "update", "started_at": NOW}, "pushed ok")

    run, = _fix_runs(store)
    assert run["stats"]["status"] == "pushed"


def test_a_transient_retry_records_no_run(store, monkeypatch):
    """A re-queued attempt has not concluded anything. Only the give-up that
    eventually ends it is a result worth a row."""
    monkeypatch.setattr(fix_worker, "TRANSIENT_EXITS", (9,))
    monkeypatch.setattr(fix_worker, "MAX_ATTEMPTS", 3)

    fix_worker._settle(1, {"action": "rebase", "attempts": 0}, 9, "the head moved")

    assert _fix_runs(store) == []
    assert store.load_pr(1).fix_request["status"] == "queued"


def test_the_give_up_after_retries_records_one_run(store, monkeypatch):
    monkeypatch.setattr(fix_worker, "TRANSIENT_EXITS", (9,))
    monkeypatch.setattr(fix_worker, "MAX_ATTEMPTS", 2)

    fix_worker._settle(1, {"action": "rebase", "attempts": 1}, 9, "the head moved")

    run, = _fix_runs(store)
    assert run["stats"]["status"] == "refused"
    assert "Gave up after 2 attempts" in run["stats"]["detail"]


# --- the history read -----------------------------------------------------------

def test_the_fix_lane_reports_its_status_action_and_detail(store):
    store.append_run({"phase": "fix:single", "pr": 1,
                      "started": "2026-07-20T01:00:00+00:00",
                      "finished": "2026-07-20T01:02:00+00:00",
                      "trigger": "autohunt",
                      "stats": {"status": "refused", "action": "rebase",
                                "detail": "conflicts in ui/src/pages/Projects.tsx",
                                "host": "mac"}})

    row, = autohunt_view.history_window(None, lanes=frozenset({"fix"}))
    assert row["phase"] == "fix"
    assert row["result"] == "refused"
    assert row["action"] == "rebase"
    assert row["detail"] == "conflicts in ui/src/pages/Projects.tsx"
    assert row["title"] == "fix boom"
    assert row["trigger"] == "autohunt"


def test_the_fix_lane_stays_out_of_the_autohunt_panel(store):
    """The auto-hunt panel reports the security and verify lanes it names. A fix
    run is a third lane with its own panel, and counting it in either of those
    would misreport both."""
    store.append_run({"phase": "fix:single", "pr": 1,
                      "stats": {"status": "pushed", "action": "update"}})
    store.append_run({"phase": "verify:single", "pr": 1,
                      "stats": {"status": "done", "outcome": "verified-fix"}})

    lanes = frozenset({"security", "verify"})
    assert [r["phase"] for r in autohunt_view.history_window(None, lanes=lanes)] == ["verify"]

    s = autohunt_view.summary(None)
    assert s["verify"]["total"] == 1
    assert s["security"]["total"] == 0


def test_a_verify_row_carries_no_fix_action(store):
    store.append_run({"phase": "verify:single", "pr": 1,
                      "stats": {"status": "done", "outcome": "verified-fix"}})

    row, = autohunt_view.history_window(None, lanes=frozenset({"verify"}))
    assert row["action"] is None
    assert row["detail"] is None


# --- the just-finished window ---------------------------------------------------

def test_a_just_finished_refusal_stays_in_the_queue_view(store):
    store.edit_pr(1).record_fix_request(
        "refused", "rebase", queued_at=_ago(200), started_at=_ago(180),
        finished_at=_ago(30), source="auto",
        refused_reason="conflicts in server/src/index.ts",
        result={"conflict_paths": ["server/src/index.ts"]})
    data.refresh()

    entry, = fix_queue.queue_entries()
    assert entry["status"] == "refused"
    assert entry["detail"] == "conflicts in server/src/index.ts"
    assert entry["conflict_paths"] == ["server/src/index.ts"]
    assert entry["finished_at"] is not None


def test_an_old_ending_has_left_the_queue_view(store):
    store.edit_pr(1).record_fix_request(
        "refused", "rebase", queued_at=_ago(9000), finished_at=_ago(8000),
        refused_reason="conflicts")
    data.refresh()

    assert fix_queue.queue_entries() == []


def test_a_failure_reports_its_error_as_the_detail(store):
    store.edit_pr(1).record_fix_request(
        "failed", "fix", queued_at=_ago(100), finished_at=_ago(10),
        error="the sandbox never started")
    data.refresh()

    entry, = fix_queue.queue_entries()
    assert entry["detail"] == "the sandbox never started"


def test_live_requests_lead_the_just_finished_ones(store):
    store.save_pr({"pr": 2, "meta": {"title": "two", "state": "open", "head_sha": "b" * 40}})
    store.edit_pr(1).record_fix_request(
        "refused", "rebase", queued_at=_ago(300), finished_at=_ago(10),
        refused_reason="conflicts")
    store.edit_pr(2).record_fix_request("queued", "update", queued_at=_ago(5))
    data.refresh()

    assert [e["pr"] for e in fix_queue.queue_entries()] == [2, 1]


def test_the_newest_ending_leads_the_finished_ones(store):
    store.save_pr({"pr": 2, "meta": {"title": "two", "state": "open", "head_sha": "b" * 40}})
    store.edit_pr(1).record_fix_request(
        "refused", "rebase", queued_at=_ago(600), finished_at=_ago(400))
    store.edit_pr(2).record_fix_request(
        "pushed", "update", queued_at=_ago(300), finished_at=_ago(20))
    data.refresh()

    assert [e["pr"] for e in fix_queue.queue_entries()] == [2, 1]


def test_an_ending_without_a_stamp_is_not_recent(store):
    """A finished_at is what dates an ending. Treating a missing one as `now`
    would pin a long-dead request to the top of the queue view forever."""
    store.edit_pr(1).record_fix_request("refused", "rebase", queued_at=_ago(9000))
    data.refresh()

    assert fix_queue.queue_entries() == []


# --- the backfill ---------------------------------------------------------------

def test_the_backfill_reconstructs_a_stored_ending(store):
    store.edit_pr(1).record_fix_request(
        "refused", "rebase", queued_at=NOW, started_at=NOW,
        finished_at="2026-06-10T00:02:00+00:00", source="auto",
        refused_reason="conflicts in server/src/index.ts", host="mac")
    data.refresh()

    entries = fix_history_backfill.backfill(fix_history_backfill.pending(), live=True)

    assert len(entries) == 1
    run, = _fix_runs(store)
    assert run["stats"] == {"status": "refused", "action": "rebase",
                            "detail": "conflicts in server/src/index.ts",
                            "host": "mac"}
    assert run["trigger"] == "autohunt"
    assert run["backfilled"] is True


def test_the_backfill_is_idempotent(store):
    store.edit_pr(1).record_fix_request(
        "refused", "rebase", queued_at=NOW, finished_at="2026-06-10T00:02:00+00:00",
        refused_reason="conflicts")
    data.refresh()
    fix_history_backfill.backfill(fix_history_backfill.pending(), live=True)

    assert fix_history_backfill.pending() == []
    fix_history_backfill.backfill(fix_history_backfill.pending(), live=True)
    assert len(_fix_runs(store)) == 1


def test_the_backfill_writes_nothing_without_live(store):
    store.edit_pr(1).record_fix_request(
        "refused", "rebase", queued_at=NOW, finished_at="2026-06-10T00:02:00+00:00",
        refused_reason="conflicts")
    data.refresh()

    entries = fix_history_backfill.backfill(fix_history_backfill.pending(), live=False)

    assert len(entries) == 1
    assert _fix_runs(store) == []


def test_the_backfill_skips_a_request_still_in_flight(store):
    store.edit_pr(1).record_fix_request("running", "rebase", queued_at=NOW,
                                        started_at=NOW)
    data.refresh()

    assert fix_history_backfill.pending() == []
