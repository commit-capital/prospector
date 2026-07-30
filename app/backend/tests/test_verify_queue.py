"""The cockpit's sandbox-verification queue: the queue/dequeue pre-checks and
state machine, runner liveness, the worker's pickup/orphan-recovery behavior,
and the request view the PR detail pane renders."""
from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import store as S
from pipeline.storekit import now as _now
from app.backend import data
from app.backend import verify_queue
from app.backend import verify_view
from app.backend import verify_worker


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    st.save_pr({"pr": 1, "meta": {"title": "fix boom", "state": "open",
                                  "head_sha": "a" * 40}})
    monkeypatch.setattr(data, "_store", st)
    data.refresh()
    return st


def test_queue_and_dequeue(store):
    assert verify_queue.queue_pr(1) == {"pr": 1, "status": "queued"}
    req = store.load_pr(1).verify_request
    assert req["status"] == "queued"
    assert req["queued_at"]
    assert verify_queue.dequeue_pr(1) == {"pr": 1, "status": "cancelled"}
    req = store.load_pr(1).verify_request
    assert req["status"] == "cancelled"
    assert req["queued_at"]  # carried across the transition


def test_queue_refuses_unknown_closed_malicious_and_inflight(store):
    with pytest.raises(ValueError, match="not in store"):
        verify_queue.queue_pr(99)

    rec = store.load_pr(1).raw
    rec["meta"]["state"] = "closed"
    store.save_pr(rec)
    with pytest.raises(ValueError, match="closed"):
        verify_queue.queue_pr(1)
    rec["meta"]["state"] = "open"
    rec["threat"] = {"verdict": "malicious", "checked_at": _now(),
                     "against_head_sha": "a" * 40}
    store.save_pr(rec)
    with pytest.raises(ValueError, match="malicious"):
        verify_queue.queue_pr(1)
    del rec["threat"]
    store.save_pr(rec)

    verify_queue.queue_pr(1)
    with pytest.raises(ValueError, match="already"):
        verify_queue.queue_pr(1)
    store.edit_pr(1).record_verify_request("running", queued_at=_now())
    with pytest.raises(ValueError, match="already"):
        verify_queue.queue_pr(1)


def test_requeue_allowed_from_terminal_states(store):
    for status in ("done", "error", "cancelled"):
        store.edit_pr(1).record_verify_request(status, queued_at=_now())
        assert verify_queue.queue_pr(1)["status"] == "queued"


def test_dequeue_only_from_queued(store):
    with pytest.raises(ValueError, match="not queued"):
        verify_queue.dequeue_pr(1)
    store.edit_pr(1).record_verify_request("running", queued_at=_now())
    with pytest.raises(ValueError, match="not queued"):
        verify_queue.dequeue_pr(1)


def test_waiting_for_base_is_in_flight(store):
    """A parked request is still in flight: it refuses a re-queue (which would
    reset the wait clock) and accepts a cancel."""
    store.edit_pr(1).record_verify_request(
        "waiting-for-base", queued_at=_now(), error_kind="no-base",
        error="no pinned base")
    with pytest.raises(ValueError, match="already"):
        verify_queue.queue_pr(1)
    assert verify_queue.dequeue_pr(1) == {"pr": 1, "status": "cancelled"}
    assert store.load_pr(1).verify_request["status"] == "cancelled"


def test_runner_status_offline_and_online(store, monkeypatch):
    s = verify_queue.runner_status()
    assert s["online"] is False and s["configured"] is False

    store.save_verify_worker({"host": "studio", "pid": 1, "current_pr": None,
                              "last_beat": datetime.now(timezone.utc).isoformat()})
    monkeypatch.setenv("TRIAGE_VERIFY_WORKER", "1")
    s = verify_queue.runner_status()
    assert s["online"] is True and s["configured"] is True and s["host"] == "studio"

    stale = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    store.save_verify_worker({"host": "studio", "pid": 1, "current_pr": None,
                              "last_beat": stale})
    assert verify_queue.runner_status()["online"] is False


def test_worker_enabled_is_an_exact_opt_in(monkeypatch):
    monkeypatch.delenv("TRIAGE_VERIFY_WORKER", raising=False)
    assert verify_worker.enabled() is False
    monkeypatch.setenv("TRIAGE_VERIFY_WORKER", "yes")
    assert verify_worker.enabled() is False
    monkeypatch.setenv("TRIAGE_VERIFY_WORKER", "1")
    assert verify_worker.enabled() is True


def test_worker_picks_oldest_queued(store):
    store.save_pr({"pr": 2, "meta": {"title": "t2", "state": "open",
                                     "head_sha": "b" * 40}})
    store.edit_pr(2).record_verify_request("queued", queued_at="2026-07-16T01:00:00+00:00")
    store.edit_pr(1).record_verify_request("queued", queued_at="2026-07-16T00:00:00+00:00")
    data.refresh()
    assert verify_worker.next_queued() == 1
    store.edit_pr(1).record_verify_request("done", queued_at="2026-07-16T00:00:00+00:00")
    data.refresh()
    assert verify_worker.next_queued() == 2
    store.edit_pr(2).record_verify_request("cancelled", queued_at="2026-07-16T01:00:00+00:00")
    data.refresh()
    assert verify_worker.next_queued() is None


def _park(store, n, *, queued_at, checked_at):
    """Write a waiting-for-base request raw, so the test controls the
    checked_at stamp the worker's rest interval reads."""
    rec = store.load_pr(n).raw
    rec["verify_request"] = {
        "status": "waiting-for-base", "queued_at": queued_at,
        "error_kind": "no-base", "error": "no pinned base",
        "checked_at": checked_at, "against_head_sha": rec["meta"]["head_sha"]}
    store.save_pr(rec)


def test_worker_retries_waiting_for_base_after_rest(store):
    rested = (datetime.now(timezone.utc)
              - timedelta(seconds=verify_worker.BASE_RETRY_SECONDS + 5)).isoformat()
    _park(store, 1, queued_at="2026-07-16T00:00:00+00:00", checked_at=rested)
    data.refresh()
    assert verify_worker.next_queued() == 1


def test_worker_rests_a_fresh_waiting_for_base(store):
    _park(store, 1, queued_at="2026-07-16T00:00:00+00:00",
          checked_at=datetime.now(timezone.utc).isoformat())
    data.refresh()
    assert verify_worker.next_queued() is None


def _requeue_transient(store, n, *, attempts, checked_at):
    """Write a transiently re-queued request raw, so the test controls the
    checked_at stamp the worker's rest interval reads."""
    rec = store.load_pr(n).raw
    rec["verify_request"] = {
        "status": "queued", "queued_at": "2026-07-16T00:00:00+00:00",
        "error_kind": "sandbox-error", "error": "isolation unproven",
        "attempts": attempts, "checked_at": checked_at,
        "against_head_sha": rec["meta"]["head_sha"]}
    store.save_pr(rec)


def test_worker_rests_a_fresh_transient_requeue(store):
    _requeue_transient(store, 1, attempts=1,
                       checked_at=datetime.now(timezone.utc).isoformat())
    data.refresh()
    assert verify_worker.next_queued() is None


def test_worker_retries_a_transient_requeue_after_rest(store):
    rested = (datetime.now(timezone.utc) - timedelta(
        seconds=verify_worker.TRANSIENT_RETRY_SECONDS + 5)).isoformat()
    _requeue_transient(store, 1, attempts=1, checked_at=rested)
    data.refresh()
    assert verify_worker.next_queued() == 1


def test_worker_picks_a_first_time_queued_request_immediately(store):
    """Only a re-queued attempt rests; a fresh queue (no attempts) is picked
    on the next drain tick regardless of how recently it was written."""
    store.edit_pr(1).record_verify_request(
        "queued", queued_at=datetime.now(timezone.utc).isoformat())
    data.refresh()
    assert verify_worker.next_queued() == 1


def test_recover_orphans_marks_only_running(store):
    store.save_pr({"pr": 2, "meta": {"title": "t2", "state": "open",
                                     "head_sha": "b" * 40}})
    store.edit_pr(1).record_verify_request("running", queued_at=_now(),
                                           started_at=_now())
    store.edit_pr(2).record_verify_request("queued", queued_at=_now())
    assert verify_worker.recover_orphans() == [1]
    req = store.load_pr(1).verify_request
    assert req["status"] == "error"
    assert req["error_kind"] == "interrupted"
    assert store.load_pr(2).verify_request["status"] == "queued"


def test_recover_orphans_leaves_another_hosts_run_alone(store):
    """A running request claimed by a different host is that worker's live
    run — this host's startup recovery must not error it."""
    store.save_pr({"pr": 2, "meta": {"title": "t2", "state": "open",
                                     "head_sha": "b" * 40}})
    store.edit_pr(1).record_verify_request("running", queued_at=_now(),
                                           started_at=_now(), host="elsewhere")
    store.edit_pr(2).record_verify_request(
        "running", queued_at=_now(), started_at=_now(),
        host=socket.gethostname())
    assert verify_worker.recover_orphans() == [2]
    assert store.load_pr(1).verify_request["status"] == "running"
    assert store.load_pr(2).verify_request["status"] == "error"


def test_finalize_leaves_another_hosts_claim_alone(store):
    """A pickup that lost the claim race exits with the winner's `running` on
    the record; finalize must not clobber the winner's live run."""
    store.edit_pr(1).record_verify_request("running", queued_at=_now(),
                                           started_at=_now(), host="elsewhere")
    verify_worker._finalize(1, 0, "lost the claim")
    assert store.load_pr(1).verify_request["status"] == "running"


def test_finalize_marks_a_crashed_pickup(store):
    store.edit_pr(1).record_verify_request("running", queued_at=_now(),
                                           started_at=_now())
    verify_worker._finalize(1, 137, "some output tail")
    req = store.load_pr(1).verify_request
    assert req["status"] == "error"
    assert req["error_kind"] == "exception"
    assert "137" in req["error"]
    assert req["log_tail"] == "some output tail"


def test_finalize_leaves_a_terminal_request_alone(store):
    store.edit_pr(1).record_verify_request("done", queued_at=_now(),
                                           finished_at=_now())
    verify_worker._finalize(1, 0, "output")
    assert store.load_pr(1).verify_request["status"] == "done"


def test_finalize_leaves_a_waiting_for_base_request_alone(store):
    """waiting-for-base is a deliberate orchestrator transition, not a crash
    leftover — the worker's finalize must not error it."""
    store.edit_pr(1).record_verify_request(
        "waiting-for-base", queued_at=_now(), error_kind="no-base",
        error="no pinned base")
    verify_worker._finalize(1, 0, "output")
    assert store.load_pr(1).verify_request["status"] == "waiting-for-base"


def test_run_one_streams_and_finalizes(store, monkeypatch):
    """run_one spawns the orchestrator argv with --from-queue and finalizes.
    The subprocess is a stub that leaves the request `running`, so _finalize
    must mark it errored."""
    store.edit_pr(1).record_verify_request("running", queued_at=_now())
    argv_seen: list[list[str]] = []

    class FakeProc:
        stdout = iter(["line one\n", "line two\n"])

        def wait(self):
            return 3

    def fake_popen(argv, **kw):
        argv_seen.append([str(a) for a in argv])
        return FakeProc()

    monkeypatch.setattr(verify_worker.subprocess, "Popen", fake_popen)
    assert verify_worker.run_one(1) == 3
    assert "--from-queue" in argv_seen[0]
    assert "--pr" in argv_seen[0]
    req = store.load_pr(1).verify_request
    assert req["status"] == "error"
    assert "line two" in req["log_tail"]


def test_request_view_renders_and_strips_ansi(store):
    store.edit_pr(1).record_verify_request(
        "error", queued_at="2026-07-16T00:00:00+00:00",
        started_at="2026-07-16T00:01:00+00:00", finished_at="2026-07-16T00:02:00+00:00",
        step="sandbox", error_kind="sandbox-error", error="boom",
        log_tail="\x1b[31mred text\x1b[0m")
    view = verify_view.verify_request_view(store.load_pr(1))
    assert view is not None
    assert view["status"] == "error"
    assert view["error_kind"] == "sandbox-error"
    assert view["log_tail"] == "red text"
    assert view["step"] == "sandbox"


def test_request_view_none_when_never_queued(store):
    assert verify_view.verify_request_view(store.load_pr(1)) is None


def test_queue_pr_source_passthrough(store):
    verify_queue.queue_pr(1, source="auto")
    assert store.load_pr(1).verify_request["source"] == "auto"
    verify_queue.dequeue_pr(1)
    verify_queue.queue_pr(1)
    assert "source" not in store.load_pr(1).verify_request


def test_source_validation_rejects_unknown(store):
    rec = store.load_pr(1).raw
    rec["verify_request"] = {"status": "queued", "queued_at": _now(),
                             "source": "gremlin", "checked_at": _now(),
                             "against_head_sha": rec["meta"]["head_sha"]}
    with pytest.raises(S.ValidationError, match="verify_request.source"):
        store.save_pr(rec)


def test_transitions_carry_source(store):
    """Every writer that replaces the verify_request section — the
    orchestrator's _Request, the worker's orphan recovery, and its crash
    finalize — keeps the source stamp."""
    from pipeline import verify_pr as VP
    req_obj = VP._Request(store, 1, _now(), "auto")
    req_obj.running("sandbox")
    assert store.load_pr(1).verify_request["source"] == "auto"
    req_obj.done()
    assert store.load_pr(1).verify_request["source"] == "auto"
    store.edit_pr(1).record_verify_request("running", queued_at=_now(), source="auto")
    verify_worker.recover_orphans()
    assert store.load_pr(1).verify_request["source"] == "auto"
    store.edit_pr(1).record_verify_request("running", queued_at=_now(), source="auto")
    verify_worker._finalize(1, 2, "tail")
    assert store.load_pr(1).verify_request["source"] == "auto"


def test_request_view_carries_source(store):
    store.edit_pr(1).record_verify_request("queued", queued_at=_now(), source="auto")
    view = verify_view.verify_request_view(store.load_pr(1))
    assert view is not None
    assert view["source"] == "auto"
