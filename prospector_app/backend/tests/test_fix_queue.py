"""The app's autofix queue: the queue/dequeue/approve pre-checks and state
machine, runner liveness, and the worker's pickup, key-safety, and
orphan-recovery behavior."""
from __future__ import annotations

import socket
import stat
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import profile
from pipeline import settings
from pipeline import store as S
from pipeline.storekit import now as _now
from prospector_app.backend import data
from prospector_app.backend import fix_queue
from prospector_app.backend import fix_worker


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    st.save_pr({"pr": 1, "meta": {"title": "fix boom", "state": "open",
                                  "head_sha": "a" * 40}})
    monkeypatch.setattr(data, "_store", st)
    data.refresh()
    return st


@pytest.fixture
def push_key(tmp_path, monkeypatch):
    key = tmp_path / "id_ed25519"
    key.write_text("key\n")
    key.chmod(0o600)
    monkeypatch.setattr(settings, "PUSH_LOGIN", "test-push-bot")
    monkeypatch.setattr(settings, "PUSH_EMAIL", "1+test-push-bot@users.noreply.github.com")
    monkeypatch.setattr(settings, "PUSH_SSH_KEY_FILE", key)
    monkeypatch.setattr(settings, "VERIFY_SCRATCH", tmp_path / "scratch")
    return key


# --- queue / dequeue / approve --------------------------------------------------

def test_queue_and_dequeue(store):
    assert fix_queue.queue_pr(1, "update") == {"pr": 1, "action": "update",
                                               "status": "queued"}
    req = store.load_pr(1).fix_request
    assert req["status"] == "queued" and req["action"] == "update"
    assert req["queued_at"]
    assert fix_queue.dequeue_pr(1) == {"pr": 1, "status": "cancelled"}
    req = store.load_pr(1).fix_request
    assert req["status"] == "cancelled"
    assert req["queued_at"]  # carried across the transition


def test_queue_refuses_unknown_action(store):
    with pytest.raises(ValueError, match="unknown autofix action"):
        fix_queue.queue_pr(1, "amend")


def test_queue_refuses_unknown_pr(store):
    with pytest.raises(ValueError, match="not in store"):
        fix_queue.queue_pr(99, "update")


def test_queue_refuses_an_ineligible_pr(store):
    rec = store.load_pr(1).raw
    rec["threat"] = {"verdict": "malicious", "checked_at": "2026-06-10T00:00:00+00:00"}
    store.save_pr(rec)
    data.refresh()
    with pytest.raises(ValueError, match="malicious"):
        fix_queue.queue_pr(1, "update")


def test_queue_refuses_a_second_request_in_flight(store):
    fix_queue.queue_pr(1, "update")
    with pytest.raises(ValueError, match="already has a queued fix request"):
        fix_queue.queue_pr(1, "rebase")


def test_dequeue_refuses_a_running_request(store):
    fix_queue.queue_pr(1, "update")
    store.claim_fix_request(1, host="h")
    data.refresh()
    with pytest.raises(ValueError, match="no cancellable fix request"):
        fix_queue.dequeue_pr(1)


def test_awaiting_review_can_be_discarded(store):
    # Cancelling an authored-but-unpushed fix is how an operator says "no".
    fix_queue.queue_pr(1, "rebase")
    store.edit_pr(1).record_fix_request("awaiting-review", "rebase",
                                        result={"patch": "diff"})
    data.refresh()
    assert fix_queue.dequeue_pr(1)["status"] == "cancelled"


def test_approve_moves_an_awaiting_review_request(store):
    fix_queue.queue_pr(1, "rebase")
    store.edit_pr(1).record_fix_request("awaiting-review", "rebase",
                                        result={"patch": "diff"},
                                        head_sha="a" * 40)
    data.refresh()
    assert fix_queue.approve_pr(1) == {"pr": 1, "status": "approved"}
    req = store.load_pr(1).fix_request
    assert req["status"] == "approved"
    assert req["result"] == {"patch": "diff"}, "the authored patch survives approval"


def test_approve_refuses_when_nothing_awaits_review(store):
    fix_queue.queue_pr(1, "rebase")
    with pytest.raises(ValueError, match="no fix awaiting review"):
        fix_queue.approve_pr(1)


def test_approve_refuses_when_the_head_moved(store):
    # The author pushed while the fix sat in review: the change no longer
    # applies to their latest head, so approval refuses rather than clobbering.
    fix_queue.queue_pr(1, "rebase")
    store.edit_pr(1).record_fix_request("awaiting-review", "rebase",
                                        result={"patch": "diff"},
                                        head_sha="a" * 40)
    rec = store.load_pr(1).raw
    rec["meta"]["head_sha"] = "b" * 40
    store.save_pr(rec)
    data.refresh()
    with pytest.raises(ValueError, match="head moved"):
        fix_queue.approve_pr(1)


# --- runner status --------------------------------------------------------------

def test_runner_status_reports_push_identity(store, push_key):
    st = fix_queue.runner_status()
    assert st["push_identity"] is True
    assert st["push_login"] == "test-push-bot"
    assert st["online"] is False and st["hosts"] == []


def test_runner_status_without_a_push_identity(store, monkeypatch):
    monkeypatch.setattr(settings, "PUSH_LOGIN", "")
    st = fix_queue.runner_status()
    assert st["push_identity"] is False
    assert st["push_login"] is None


def test_a_laptop_can_queue_while_the_worker_holds_the_key(store, monkeypatch):
    # The whole point of a shared queue: an operator clicks from a machine with
    # no push credential at all, and the machine that has one does the work.
    # can_queue must follow the WORKER, never this backend's own config.
    monkeypatch.setattr(settings, "PUSH_LOGIN", "")
    store.save_fix_worker({"host": "sandbox-box", "last_beat": _now(),
                           "current_pr": None, "autohunt": False})
    st = fix_queue.runner_status()
    assert st["push_identity"] is False, "this backend holds no key"
    assert st["online"] is True
    assert st["can_queue"] is True, "a laptop must still be able to queue"


def test_cannot_queue_when_no_worker_has_ever_been_seen(store, monkeypatch):
    monkeypatch.setattr(settings, "PUSH_LOGIN", "")
    st = fix_queue.runner_status()
    assert st["can_queue"] is False


def test_the_worker_machine_can_queue_before_its_first_beat(store, push_key):
    # The sandbox box itself holds the key, so it can queue even with no
    # heartbeat written yet.
    st = fix_queue.runner_status()
    assert st["online"] is False
    assert st["can_queue"] is True


def test_a_stale_worker_beat_stops_queueing(store, monkeypatch):
    # A worker that stopped beating cannot drain, so a queued action would sit
    # forever — say so rather than accepting the click.
    monkeypatch.setattr(settings, "PUSH_LOGIN", "")
    old_beat = (datetime.now(timezone.utc)
                - timedelta(seconds=fix_queue.STALE_BEAT_SECONDS + 60)).isoformat()
    store.save_fix_worker({"host": "sandbox-box", "last_beat": old_beat,
                           "current_pr": None, "autohunt": False})
    st = fix_queue.runner_status()
    assert st["online"] is False and st["can_queue"] is False


# --- worker: key safety is asserted, not assumed --------------------------------

def test_key_safety_passes_for_a_private_key_outside_the_scratch_root(push_key):
    assert fix_worker.key_safety_failure() is None


def test_key_safety_refuses_an_unconfigured_identity(monkeypatch):
    monkeypatch.setattr(settings, "PUSH_LOGIN", "")
    assert "no contributor-push identity" in (fix_worker.key_safety_failure() or "")


def test_key_safety_refuses_a_group_readable_key(push_key):
    push_key.chmod(0o640)
    assert "group/world-accessible" in (fix_worker.key_safety_failure() or "")


def test_key_safety_refuses_a_key_under_the_sandbox_scratch_root(tmp_path, monkeypatch):
    # The worker machine also runs untrusted contributor code in the sandbox, so
    # the push credential must live outside anything the sandbox can reach.
    scratch = tmp_path / "scratch"
    (scratch / "keys").mkdir(parents=True)
    key = scratch / "keys" / "id_ed25519"
    key.write_text("key\n")
    key.chmod(0o600)
    monkeypatch.setattr(settings, "PUSH_LOGIN", "test-push-bot")
    monkeypatch.setattr(settings, "PUSH_EMAIL", "1+b@users.noreply.github.com")
    monkeypatch.setattr(settings, "PUSH_SSH_KEY_FILE", key)
    monkeypatch.setattr(settings, "VERIFY_SCRATCH", scratch)
    assert "sandbox scratch root" in (fix_worker.key_safety_failure() or "")


def test_key_safety_refuses_a_missing_key(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PUSH_LOGIN", "test-push-bot")
    monkeypatch.setattr(settings, "PUSH_EMAIL", "1+b@users.noreply.github.com")
    monkeypatch.setattr(settings, "PUSH_SSH_KEY_FILE", tmp_path / "absent")
    monkeypatch.setattr(settings, "VERIFY_SCRATCH", tmp_path / "scratch")
    assert "unreadable" in (fix_worker.key_safety_failure() or "")


def test_startup_refuses_an_unsafe_key_without_taking_the_app_down(push_key, monkeypatch,
                                                                   capsys):
    push_key.chmod(0o644)
    monkeypatch.setattr(settings, "FIX_WORKER", True)
    assert fix_worker.startup() is False
    assert "NOT started" in capsys.readouterr().out


def test_startup_skips_when_this_backend_is_not_the_runner(monkeypatch):
    monkeypatch.setattr(settings, "FIX_WORKER", False)
    assert fix_worker.startup() is False


# --- worker: pickup order and orphan recovery -----------------------------------

def test_operator_picks_beat_auto_picks(store):
    store.save_pr({"pr": 2, "meta": {"title": "t", "state": "open", "head_sha": "b" * 40}})
    data.refresh()
    fix_queue.queue_pr(2, "update", source="auto")
    fix_queue.queue_pr(1, "update")
    assert fix_worker.next_queued() == 1


def test_approved_requests_are_picked_up(store):
    fix_queue.queue_pr(1, "rebase")
    store.edit_pr(1).record_fix_request("approved", "rebase", result={"patch": "d"})
    data.refresh()
    assert fix_worker.next_approved() == 1
    assert fix_worker.next_queued() is None


def test_orphan_recovery_marks_this_hosts_running_requests_failed(store):
    fix_queue.queue_pr(1, "update")
    store.claim_fix_request(1, host=socket.gethostname())
    data.refresh()
    assert fix_worker.recover_orphans() == [1]
    req = store.load_pr(1).fix_request
    assert req["status"] == "failed"
    assert "nothing was pushed" in req["error"]


def test_orphan_recovery_leaves_another_hosts_run_alone(store):
    fix_queue.queue_pr(1, "update")
    store.claim_fix_request(1, host="some-other-machine")
    data.refresh()
    assert fix_worker.recover_orphans() == []
    assert store.load_pr(1).fix_request["status"] == "running"


def test_orphan_recovery_flags_an_interrupted_push_as_indeterminate(store):
    # A push may or may not have landed before the process died; the worker
    # says so rather than guessing.
    fix_queue.queue_pr(1, "rebase")
    store.edit_pr(1).record_fix_request("pushing", "rebase", host=socket.gethostname())
    data.refresh()
    assert fix_worker.recover_orphans() == [1]
    assert "re-read the PR" in store.load_pr(1).fix_request["error"]


# --- worker: the idle hunter only takes mechanical actions ----------------------

def test_hunter_queues_a_rebase_for_an_unmergeable_pr(store):
    rec = store.load_pr(1).raw
    rec["signals"] = {"mergeable": False, "checked_at": "2026-06-10T00:00:00+00:00",
                      "against_head_sha": "a" * 40}
    store.save_pr(rec)
    data.refresh()
    assert fix_worker.next_auto() == ("rebase", 1)


def test_hunter_never_queues_an_agent_authored_fix(store, monkeypatch):
    monkeypatch.setattr(
        profile, "active",
        lambda: profile.RepoProfile(autofix=profile.AutofixPolicy(fixable_gates=("ci",))))
    rec = store.load_pr(1).raw
    rec["signals"] = {"ci": "failing", "checked_at": "2026-06-10T00:00:00+00:00",
                      "against_head_sha": "a" * 40}
    store.save_pr(rec)
    data.refresh()
    assert fix_worker.next_auto() is None


def test_hunter_skips_a_pr_already_in_flight(store):
    rec = store.load_pr(1).raw
    rec["signals"] = {"mergeable": False, "checked_at": "2026-06-10T00:00:00+00:00",
                      "against_head_sha": "a" * 40}
    store.save_pr(rec)
    data.refresh()
    fix_queue.queue_pr(1, "rebase")
    assert fix_worker.next_auto() is None


def test_key_mode_helper_uses_owner_only_permissions(push_key):
    # Guards the fixture itself: a 600 key is what the safety check accepts.
    assert stat.S_IMODE(push_key.stat().st_mode) == 0o600
