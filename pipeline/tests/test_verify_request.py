"""The verify_request section (the operator's sandbox-verification queue state)
and the verify_worker heartbeat registry."""
from __future__ import annotations

import pytest

from pipeline import store as S
from pipeline.storekit import ValidationError, now as _now


def _base_pr(n=1, head="sha1"):
    return {"pr": n, "meta": {"title": "t", "state": "open", "head_sha": head}}


def _seed(tmp_path, rec):
    st = S.Store(tmp_path)
    st.save_pr(rec)
    return st


def test_record_verify_request_round_trip(tmp_path):
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).record_verify_request("queued", queued_at="2026-07-16T00:00:00+00:00")
    req = st.load_pr(1).verify_request
    assert req is not None
    assert req["status"] == "queued"
    assert req["queued_at"] == "2026-07-16T00:00:00+00:00"
    assert req["against_head_sha"] == "sha1"
    assert "checked_at" in req


def test_record_verify_request_replaces_whole_section(tmp_path):
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).record_verify_request("queued", queued_at="2026-07-16T00:00:00+00:00")
    st.edit_pr(1).record_verify_request(
        "error", queued_at="2026-07-16T00:00:00+00:00",
        started_at="2026-07-16T00:01:00+00:00", finished_at="2026-07-16T00:02:00+00:00",
        error_kind="no-base", error="no pinned base", log_tail="tail")
    req = st.load_pr(1).verify_request
    assert req["status"] == "error"
    assert req["error_kind"] == "no-base"
    assert req["error"] == "no pinned base"
    assert req["log_tail"] == "tail"
    assert req["started_at"] == "2026-07-16T00:01:00+00:00"


def test_record_verify_request_omits_unset_fields(tmp_path):
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).record_verify_request("queued", queued_at="2026-07-16T00:00:00+00:00")
    req = st.load_pr(1).verify_request
    assert "error_kind" not in req
    assert "log_tail" not in req
    assert "step" not in req


def test_record_verify_request_accepts_waiting_for_base(tmp_path):
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).record_verify_request(
        "waiting-for-base", queued_at="2026-07-16T00:00:00+00:00",
        error_kind="no-base", error="no pinned base")
    req = st.load_pr(1).verify_request
    assert req["status"] == "waiting-for-base"
    assert req["error_kind"] == "no-base"
    assert req["error"] == "no pinned base"
    assert req["queued_at"] == "2026-07-16T00:00:00+00:00"


def test_verify_request_none_when_never_queued(tmp_path):
    st = _seed(tmp_path, _base_pr())
    assert st.load_pr(1).verify_request is None


def test_record_verify_request_carries_attempts(tmp_path):
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).record_verify_request(
        "queued", queued_at="2026-07-16T00:00:00+00:00",
        error_kind="fetch-error", error="cannot fetch patch", attempts=2)
    req = st.load_pr(1).verify_request
    assert req["status"] == "queued"
    assert req["error_kind"] == "fetch-error"
    assert req["attempts"] == 2


def test_claim_carries_attempts(tmp_path):
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).record_verify_request(
        "queued", queued_at="2026-07-16T00:00:00+00:00",
        error_kind="agent-failed", error="the blind adequacy agent failed",
        attempts=1)
    sec = st.claim_verify_request(1, host="studio")
    assert sec is not None
    assert sec["attempts"] == 1


def test_validate_rejects_unknown_status(tmp_path):
    st = _seed(tmp_path, _base_pr())
    rec = st.load_pr(1).raw
    rec["verify_request"] = {"status": "sideways", "checked_at": "now",
                             "against_head_sha": "sha1"}
    with pytest.raises(ValidationError):
        st.save_pr(rec)


def test_validate_rejects_unknown_error_kind(tmp_path):
    st = _seed(tmp_path, _base_pr())
    rec = st.load_pr(1).raw
    rec["verify_request"] = {"status": "error", "error_kind": "gremlins",
                             "checked_at": "now", "against_head_sha": "sha1"}
    with pytest.raises(ValidationError):
        st.save_pr(rec)


def test_verify_request_is_a_known_section(tmp_path, capsys):
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).record_verify_request("queued", queued_at="2026-07-16T00:00:00+00:00")
    assert "unknown section" not in capsys.readouterr().err


def test_verify_worker_registry_round_trip(tmp_path):
    st = S.Store(tmp_path)
    assert st.load_verify_worker() == {"hosts": {}}
    st.save_verify_worker({"host": "studio", "pid": 42,
                           "last_beat": "2026-07-16T00:00:00+00:00", "current_pr": 7})
    reg = st.load_verify_worker()
    assert reg["hosts"]["studio"]["current_pr"] == 7


def test_verify_worker_registry_keeps_one_record_per_host(tmp_path):
    st = S.Store(tmp_path)
    st.save_verify_worker({"host": "studio", "pid": 42,
                           "last_beat": _now(), "current_pr": 7})
    st.save_verify_worker({"host": "laptop", "pid": 43,
                           "last_beat": _now(), "current_pr": None})
    last = _now()
    st.save_verify_worker({"host": "studio", "pid": 42,
                           "last_beat": last, "current_pr": None})
    reg = st.load_verify_worker()
    assert set(reg["hosts"]) == {"studio", "laptop"}
    assert reg["hosts"]["studio"]["last_beat"] == last
    assert reg["hosts"]["laptop"]["pid"] == 43


def test_verify_worker_registry_prunes_week_stale_hosts(tmp_path):
    st = S.Store(tmp_path)
    st.save_verify_worker({"host": "old-studio", "pid": 41,
                           "last_beat": "2020-01-01T00:00:00+00:00",
                           "current_pr": None})
    st.save_verify_worker({"host": "laptop", "pid": 43,
                           "last_beat": _now(), "current_pr": None})
    assert set(st.load_verify_worker()["hosts"]) == {"laptop"}


def test_verify_worker_registry_migrates_a_flat_record_on_read(tmp_path):
    st = S.Store(tmp_path)
    st._save_registry("verify_worker", {
        "host": "studio", "pid": 42,
        "last_beat": "2026-07-16T00:00:00+00:00", "current_pr": 7})
    reg = st.load_verify_worker()
    assert reg["hosts"]["studio"]["current_pr"] == 7


def test_claim_takes_a_queued_request_exactly_once(tmp_path):
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).record_verify_request("queued", queued_at=_now(), source="auto")
    sec = st.claim_verify_request(1, host="studio")
    assert sec is not None
    assert sec["status"] == "running"
    assert sec["step"] == "claimed"
    assert sec["host"] == "studio"
    assert sec["source"] == "auto"
    assert sec["queued_at"]
    req = st.load_pr(1).verify_request or {}
    assert req["status"] == "running"
    assert req["host"] == "studio"
    assert st.claim_verify_request(1, host="laptop") is None


def test_claim_takes_a_waiting_for_base_request(tmp_path):
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).record_verify_request("waiting-for-base", queued_at=_now(),
                                        error_kind="no-base", error="no pin")
    assert st.claim_verify_request(1, host="studio") is not None


def test_claim_refuses_terminal_and_missing_requests(tmp_path):
    st = _seed(tmp_path, _base_pr())
    assert st.claim_verify_request(1, host="studio") is None
    st.edit_pr(1).record_verify_request("done", queued_at=_now(), finished_at=_now())
    assert st.claim_verify_request(1, host="studio") is None
    assert st.claim_verify_request(999, host="studio") is None


def test_claim_loses_to_a_concurrent_writer(tmp_path, monkeypatch):
    # The compare-and-swap half: a write landing between the claim's read and
    # its conditional save invalidates the stamp, and the claim reports lost.
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).record_verify_request("queued", queued_at=_now())
    stale = st._prs.stamped(1)
    st.edit_pr(1).record_verify_request("cancelled", queued_at=_now(),
                                        finished_at=_now())
    monkeypatch.setattr(st._prs, "stamped", lambda n: stale)
    assert st.claim_verify_request(1, host="studio") is None
    assert (st.load_pr(1).verify_request or {})["status"] == "cancelled"


def test_verify_worker_registry_requires_last_beat(tmp_path):
    st = S.Store(tmp_path)
    with pytest.raises(ValidationError):
        st.save_verify_worker({"host": "studio", "pid": 42, "current_pr": None})
