"""gates.verify_retry_allowed: when the hunter may re-run an errored verify."""
from datetime import datetime, timedelta, timezone

from pipeline import gates
from pipeline.model import Pr

HEAD = "a" * 40
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _pr(status="error", kind="sandbox-error", host="studio", attempts=0,
        finished=NOW - timedelta(minutes=5), head=HEAD):
    return Pr(None, {"pr": 1, "meta": {"state": "open", "head_sha": head},
               "verify_request": {"status": status, "error_kind": kind, "host": host,
                                  "attempts": attempts, "finished_at": finished.isoformat(),
                                  "against_head_sha": HEAD}})


def test_a_different_worker_may_retry_at_once():
    ok, why = gates.verify_retry_allowed(_pr(), worker="laptop", now=NOW)
    assert ok and "different worker" in why


def test_the_same_worker_waits_six_hours():
    assert not gates.verify_retry_allowed(_pr(), worker="studio", now=NOW)[0]
    later = NOW + timedelta(hours=6, minutes=1)
    assert gates.verify_retry_allowed(_pr(), worker="studio", now=later)[0]


def test_a_moved_head_retries_anywhere():
    pr = _pr(head="b" * 40)
    ok, why = gates.verify_retry_allowed(pr, worker="studio", now=NOW)
    assert ok and "head moved" in why


def test_the_prs_own_fault_never_retries():
    assert not gates.verify_retry_allowed(_pr(kind="refused-safety"), worker="laptop", now=NOW)[0]
    assert not gates.verify_retry_allowed(_pr(status="cancelled"), worker="laptop", now=NOW)[0]


def test_the_head_run_cap_holds():
    assert not gates.verify_retry_allowed(
        _pr(attempts=gates.VERIFY_RETRY_ATTEMPTS - 1), worker="laptop", now=NOW)[0]
