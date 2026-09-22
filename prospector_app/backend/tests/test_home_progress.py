"""Tests for home_progress.progress() — the Home progress row's four tiles."""
from datetime import date, timedelta, timezone

from pipeline import storekit
from prospector_app.backend import home_progress

_TODAY = date(2026, 6, 24)


def _days_ago(n: int) -> str:
    return (_TODAY - timedelta(days=n)).isoformat()


def _ev(at: str, kind: str, pr: int | None = None, **extra) -> dict:
    return {"at": at, "kind": kind, "status": "executed", "dry_run": False,
            **({"pr": pr} if pr is not None else {}), **extra}


def _pr(n: int, created_at: str, state: str = "open") -> object:
    class FakePr:
        def __init__(self):
            self.state = state
            self.created_at = created_at
    return FakePr()


def _run(phase: str, at: str, pr: int | None = None, **stats) -> storekit.RunRecord:
    rec: dict = {"phase": phase, "started": at, "finished": at}
    if pr is not None:
        rec["pr"] = pr
    if stats:
        rec["stats"] = stats
    return storekit.parse_run(rec)


def _progress(prs=None, issues=None, events=None, runs=None) -> dict:
    return home_progress.progress(prs or {}, issues or [], events or [],
                                  runs or [], today=_TODAY, tz=timezone.utc)


def test_backlog_series_walks_back_from_todays_true_count():
    prs = {
        1: _pr(1, f"{_days_ago(2)}T10:00:00Z"),
        2: _pr(2, f"{_days_ago(1)}T10:00:00Z"),
        3: _pr(3, f"{_days_ago(1)}T11:00:00Z", state="closed"),
    }
    events = [_ev(f"{_days_ago(1)}T12:00:00Z", "close", pr=3)]
    out = _progress(prs=prs, events=events)
    b = out["backlog"]
    assert b["current"] == 2
    # Yesterday: 2 PRs in, 1 closed (net +1); the day before: 1 in.
    assert b["series"][-1] == 2
    assert b["series"][-2] == 2  # today saw no flow
    assert b["series"][-3] == 1  # before yesterday's net +1
    assert b["delta_7d"] == 2 - b["series"][-8]


def test_open_issues_count_toward_backlog():
    issues = [{"created_at": f"{_days_ago(3)}T08:00:00Z", "state": "open"},
              {"created_at": f"{_days_ago(3)}T09:00:00Z", "state": "closed"}]
    assert _progress(issues=issues)["backlog"]["current"] == 1


def test_resolved_week_splits_side_effect_closes_from_clicks():
    events = [
        _ev(f"{_days_ago(1)}T10:00:00Z", "merge", pr=1),
        _ev(f"{_days_ago(1)}T10:00:01Z", "issue-close", via="merge"),
        _ev(f"{_days_ago(9)}T10:00:00Z", "close", pr=2),
        _ev(f"{_days_ago(2)}T10:00:00Z", "reopen", pr=3),
        {"at": f"{_days_ago(1)}T11:00:00Z", "kind": "close", "pr": 4,
         "status": "error", "dry_run": False},
    ]
    r = _progress(events=events)["resolved"]
    assert r["week_total"] == 2  # the merge and its side-effect close; not the
    assert r["prev_week_total"] == 1  # week-old close, the reopen, or the error
    assert r["auto_7d"] == 1
    assert r["person_7d"] == 1


def test_escalation_rate_counts_lane_decisions():
    runs = [
        _run("fix:single", f"{_days_ago(1)}T10:00:00Z", pr=1, status="pushed"),
        _run("fix:single", f"{_days_ago(1)}T11:00:00Z", pr=2, status="awaiting-review"),
        _run("fix:single", f"{_days_ago(1)}T12:00:00Z", pr=3, status="failed"),
        _run("verify:single", f"{_days_ago(2)}T10:00:00Z", pr=4, status="done",
             outcome="verified-fix"),
        _run("verify:single", f"{_days_ago(2)}T11:00:00Z", pr=5, status="done",
             outcome="escalate"),
        _run("security:review-one", f"{_days_ago(3)}T10:00:00Z", pr=6, verdict="RED"),
        _run("security:review-one", f"{_days_ago(3)}T11:00:00Z", pr=7, verdict="GREEN"),
    ]
    e = _progress(runs=runs)["escalation"]
    # failed is a machine fault, not a decision; the park, the escalate outcome,
    # and the RED verdict each hand work to a person.
    assert e["decisions_7d"] == 6
    assert e["escalated_7d"] == 3
    assert e["rate_7d"] == 0.5
    assert e["prev_rate_7d"] is None


def test_escalation_rate_is_null_without_decisions():
    e = _progress()["escalation"]
    assert e["rate_7d"] is None
    assert all(v is None for v in e["series"])


def test_first_action_median_from_earliest_touch():
    prs = {
        1: _pr(1, f"{_days_ago(2)}T10:00:00Z"),
        2: _pr(2, f"{_days_ago(2)}T10:00:00Z"),
        3: _pr(3, f"{_days_ago(2)}T10:00:00Z"),  # never touched
    }
    events = [_ev(f"{_days_ago(2)}T12:00:00Z", "comment", pr=1),
              _ev(f"{_days_ago(1)}T10:00:00Z", "close", pr=1)]
    runs = [_run("security:review-one", f"{_days_ago(2)}T16:00:00Z", pr=2, verdict="GREEN")]
    f = _progress(prs=prs, events=events, runs=runs)["first_action"]
    # PR 1's first touch is the comment (2h), PR 2's the review (6h).
    assert f["median_hours_7d"] == 4.0
    assert f["sampled_7d"] == 2
    day_i = _progress()["days"].index(_days_ago(2))
    assert f["series"][day_i] == 4.0


def test_last_ingest_at_reads_the_newest_ingest_run():
    runs = [_run("ingest", f"{_days_ago(20)}T10:00:00Z"),
            _run("ingest", f"{_days_ago(19)}T10:00:00Z"),
            _run("fix:single", f"{_days_ago(1)}T10:00:00Z", pr=1, status="pushed")]
    out = _progress(runs=runs)
    assert out["last_ingest_at"] == f"{_days_ago(19)}T10:00:00Z"
    assert _progress()["last_ingest_at"] is None
