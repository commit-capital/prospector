"""The "Done on its own" feed: worker-initiated landed actions only, each with
its available undo."""
from __future__ import annotations

from prospector_app.backend import autonomous_feed


def _ev(kind: str, *, initiator: str | None = "worker", status: str = "executed",
        dry_run: bool = False, **extra) -> dict:
    ev = {"at": "2026-09-22T10:00:00+00:00", "kind": kind, "status": status,
          "dry_run": dry_run, **extra}
    if initiator is not None:
        ev["initiator"] = initiator
    return ev


def test_feed_keeps_only_worker_initiated_events():
    events = [
        _ev("resubmit", pr=1, action="RESUBMIT"),
        _ev("resubmit", pr=2, action="RESUBMIT", initiator="operator"),
        _ev("close", pr=3, initiator=None),
    ]
    assert [i["pr"] for i in autonomous_feed.feed(events=events)] == [1]


def test_feed_excludes_dry_runs_and_failures():
    events = [
        _ev("resubmit", pr=1, action="RESUBMIT", dry_run=True, status="dry-run"),
        _ev("review_retrigger", pr=2, status="error"),
        _ev("review_retrigger", pr=3, status="skipped"),
        _ev("review_retrigger", pr=4),
    ]
    assert [i["pr"] for i in autonomous_feed.feed(events=events)] == [4]


def test_feed_names_the_undo_for_closes():
    events = [
        _ev("close", pr=1),
        _ev("issue-close", issue=9),
        _ev("resubmit", pr=2, action="RESUBMIT"),
        _ev("review_retrigger", pr=3),
    ]
    undos = {(i.get("pr"), i.get("issue")): i["undo"] for i in autonomous_feed.feed(events=events)}
    assert undos[(1, None)] == "reopen-pr"
    assert undos[(None, 9)] == "reopen-issue"
    assert undos[(2, None)] is None
    assert undos[(3, None)] is None


def test_feed_item_carries_who_what_when():
    ev = _ev("resubmit", pr=7, action="UPDATE_BRANCH", identity="prospector-bot",
             detail="merged the base in")
    row, = autonomous_feed.feed(events=[ev])
    assert row["at"] == "2026-09-22T10:00:00+00:00"
    assert row["kind"] == "resubmit"
    assert row["action"] == "UPDATE_BRANCH"
    assert row["identity"] == "prospector-bot"
    assert row["detail"] == "merged the base in"


def test_feed_honors_the_limit():
    events = [_ev("resubmit", pr=n, action="RESUBMIT") for n in range(10)]
    assert len(autonomous_feed.feed(limit=3, events=events)) == 3


def test_retrigger_records_its_initiator():
    """The executor's re-trigger stamps who asked, so the hunter's mentions read
    as the automation's own and an operator's click does not."""
    from prospector_app.backend import executor
    res = executor.retrigger_review(1, "nonexistent-reviewer", token=None,
                                    dry_run=True, initiator="worker")
    assert res["initiator"] == "worker"
    default = executor.retrigger_review(1, "nonexistent-reviewer", token=None, dry_run=True)
    assert default["initiator"] == "operator"


def test_unattended_resubmit_env_carries_the_worker_marker(monkeypatch):
    """A push made on the automation's own judgment marks its resubmit
    subprocess, and an approved push does not."""
    import subprocess
    from prospector_app.backend import fix_worker
    from prospector_app.backend import resubmit_identity
    seen: list[dict[str, str]] = []

    def fake_run(argv, **kw):
        seen.append(kw.get("env") or {})
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fix_worker._resubmit(1, "state", unattended=True)
    fix_worker._resubmit(1, "state")
    assert resubmit_identity.initiator(seen[0]) == "worker"
    assert resubmit_identity.initiator(seen[1]) == "operator"
    assert resubmit_identity.uses_machine_user(seen[0])
    assert resubmit_identity.uses_machine_user(seen[1])
