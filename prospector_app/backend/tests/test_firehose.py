"""Tests for activity.firehose_stats() and activity.reopened_after_close() (#238)."""
from datetime import date, timedelta, timezone
from prospector_app.backend import activity


def _ev(at: str, kind: str, pr: int | None = None, **extra) -> dict:
    return {"at": at, "kind": kind, "status": "executed", "dry_run": False,
            **({"pr": pr} if pr is not None else {}), **extra}


def _pr(n: int, created_at: str, state: str = "open", title: str = "PR", author: str = "author") -> object:
    """Minimal Pr-like object for testing."""
    class FakePr:
        def __init__(self):
            self.state = state
            self.created_at = created_at
            self.title = title
            self.url = f"https://github.com/test-owner/test-repo/pull/{n}"
            self.author = author
    return FakePr()


def _issue(created_at: str) -> dict:
    return {"created_at": created_at, "title": "Issue", "state": "open"}


# Fixed "today" for deterministic tests — use a date far enough back that
# our test events fall within a 30-day window.
_TODAY = date(2026, 6, 24)


def _days_ago(n: int) -> str:
    return (_TODAY - timedelta(days=n)).isoformat()


# ── firehose_stats ────────────────────────────────────────────────────────────

def test_firehose_pr_incoming_counted_by_created_at():
    prs = {
        1: _pr(1, f"{_days_ago(2)}T10:00:00Z"),  # 2 days ago
        2: _pr(2, f"{_days_ago(2)}T11:00:00Z"),  # same day
        3: _pr(3, f"{_days_ago(10)}T09:00:00Z"), # 10 days ago
        4: _pr(4, "2020-01-01T00:00:00Z"),        # way outside window
    }
    stats = activity.firehose_stats(prs, [], n_days=30, events=[], today=_TODAY, tz=timezone.utc)
    day_idx = {d: i for i, d in enumerate(stats["days"])}
    two_days_ago = _days_ago(2)
    assert stats["pr_incoming"][day_idx[two_days_ago]] == 2
    assert stats["pr_incoming"][day_idx[_days_ago(10)]] == 1
    # PRs outside window are not counted
    assert sum(stats["pr_incoming"]) == 3


def test_firehose_issue_incoming_counted():
    issues = [
        _issue(f"{_days_ago(1)}T08:00:00Z"),
        _issue(f"{_days_ago(1)}T09:00:00Z"),
    ]
    stats = activity.firehose_stats({}, issues, n_days=30, events=[], today=_TODAY, tz=timezone.utc)
    day_idx = {d: i for i, d in enumerate(stats["days"])}
    assert stats["iss_incoming"][day_idx[_days_ago(1)]] == 2
    assert sum(stats["iss_incoming"]) == 2


def test_firehose_triaged_from_events():
    events = [
        _ev(f"{_days_ago(3)}T10:00:00+00:00", "close", pr=100),
        _ev(f"{_days_ago(3)}T11:00:00+00:00", "merge", pr=101),
        _ev(f"{_days_ago(3)}T12:00:00+00:00", "comment", pr=102),  # not a triage action
        _ev(f"{_days_ago(3)}T13:00:00+00:00", "close", dry_run=True, pr=103),  # dry-run
    ]
    stats = activity.firehose_stats({}, [], n_days=30, events=events, today=_TODAY, tz=timezone.utc)
    day_idx = {d: i for i, d in enumerate(stats["days"])}
    three_ago = _days_ago(3)
    # Only close+merge land; comment and dry-run are excluded
    assert stats["pr_triaged"][day_idx[three_ago]] == 2
    assert sum(stats["pr_triaged"]) == 2
    # Granular breakdown: 1 close, 1 merge
    assert stats["pr_closed"][day_idx[three_ago]] == 1
    assert stats["pr_merged"][day_idx[three_ago]] == 1
    assert sum(stats["pr_closed"]) == 1
    assert sum(stats["pr_merged"]) == 1


def test_firehose_merge_buckets_by_operator_local_day_not_utc():
    """A merge stamped just after UTC midnight lands on the operator's *local*
    calendar day, not the UTC day. The store stamps ``at`` in UTC, but the
    app shows it in local time — so an evening merge (already "tomorrow" in
    UTC) must bucket on today's local bar, or it falls a day past the window's
    leading edge and vanishes from the count (the 4-vs-5 bug)."""
    pacific = timezone(timedelta(hours=-8))
    # 00:58 UTC on the 26th is 16:58 on the 25th in Pacific.
    events = [_ev("2026-06-26T00:58:43+00:00", "merge", pr=3800, status="merged")]
    stats = activity.firehose_stats(
        {}, [], n_days=30, events=events, today=date(2026, 6, 25), tz=pacific)
    day_idx = {d: i for i, d in enumerate(stats["days"])}
    # Bucketed under the local day (the 25th) — the window's last day — not the
    # UTC day (the 26th), which would be one past the window's edge.
    assert "2026-06-26" not in day_idx
    assert stats["pr_merged"][day_idx["2026-06-25"]] == 1
    assert sum(stats["pr_merged"]) == 1
    assert stats["totals"]["pr_merged_nd"] == 1


def test_firehose_incoming_buckets_by_operator_local_day():
    """Incoming PRs/issues bucket in the operator's local timezone too, so every
    series on the chart shares one day-boundary basis."""
    pacific = timezone(timedelta(hours=-8))
    prs = {1: _pr(1, "2026-06-26T02:00:00Z")}        # 18:00 the 25th, Pacific
    issues = [_issue("2026-06-26T03:00:00Z")]          # 19:00 the 25th, Pacific
    stats = activity.firehose_stats(
        prs, issues, n_days=30, events=[], today=date(2026, 6, 25), tz=pacific)
    day_idx = {d: i for i, d in enumerate(stats["days"])}
    assert stats["pr_incoming"][day_idx["2026-06-25"]] == 1
    assert stats["iss_incoming"][day_idx["2026-06-25"]] == 1


def test_firehose_issue_closed_from_events():
    events = [
        _ev(f"{_days_ago(2)}T10:00:00+00:00", "issue-close", issue=500, status="closed"),
        _ev(f"{_days_ago(2)}T11:00:00+00:00", "issue-close", issue=501, status="closed"),
        _ev(f"{_days_ago(9)}T10:00:00+00:00", "issue-close", issue=502, status="closed"),
        _ev(f"{_days_ago(3)}T10:00:00+00:00", "issue-close", issue=503, status="dry-run", dry_run=True),
        _ev(f"{_days_ago(3)}T11:00:00+00:00", "issue-close", issue=504, status="blocked"),
    ]
    stats = activity.firehose_stats({}, [], n_days=30, events=events, today=_TODAY, tz=timezone.utc)
    day_idx = {d: i for i, d in enumerate(stats["days"])}
    # Only landed closes count; the dry-run and blocked attempts are excluded
    assert stats["iss_closed"][day_idx[_days_ago(2)]] == 2
    assert stats["iss_closed"][day_idx[_days_ago(9)]] == 1
    assert sum(stats["iss_closed"]) == 3
    assert stats["totals"]["iss_closed_7d"] == 2   # day 9 is outside the last-7 window
    assert stats["totals"]["iss_closed_nd"] == 3


def test_firehose_issue_closed_counts_scoped_input_events():
    prs = {1: _pr(1, f"{_days_ago(2)}T10:00:00Z", author="alice")}
    events = [_ev(f"{_days_ago(2)}T12:00:00+00:00", "issue-close", issue=600, status="closed")]
    stats = activity.firehose_stats(
        prs, [], n_days=30, events=events, today=_TODAY, tz=timezone.utc)
    assert sum(stats["iss_closed"]) == 1


def test_firehose_reopened_from_events():
    events = [
        _ev(f"{_days_ago(2)}T10:00:00+00:00", "reopen", pr=200),
        _ev(f"{_days_ago(2)}T11:00:00+00:00", "reopen", pr=201),
        _ev(f"{_days_ago(5)}T10:00:00+00:00", "reopen", pr=202),
    ]
    stats = activity.firehose_stats({}, [], n_days=30, events=events, today=_TODAY, tz=timezone.utc)
    day_idx = {d: i for i, d in enumerate(stats["days"])}
    assert stats["pr_reopened"][day_idx[_days_ago(2)]] == 2
    assert stats["pr_reopened"][day_idx[_days_ago(5)]] == 1
    assert sum(stats["pr_reopened"]) == 3


def test_firehose_7d_totals():
    # PRs created 1-9 days ago; last7 = today through 6 days ago (7 entries)
    prs = {i: _pr(i, f"{_days_ago(i)}T10:00:00Z") for i in range(1, 10)}
    events = [_ev(f"{_days_ago(i)}T10:00:00+00:00", "close", pr=i) for i in range(1, 4)]
    stats = activity.firehose_stats(prs, [], n_days=30, events=events, today=_TODAY, tz=timezone.utc)
    totals = stats["totals"]
    assert totals["pr_incoming_7d"] == 6   # prs 1-6 (days ago 1-6) — day 7 is outside
    assert totals["pr_triaged_7d"] == 3    # closes on days 1,2,3
    assert totals["pr_closed_7d"] == 3     # same closes
    assert totals["pr_merged_7d"] == 0
    assert totals["pr_incoming_30d"] == 9  # prs 1-9 within 30 days
    # nd fields reflect the full n_days window
    assert totals["pr_incoming_nd"] == 9
    assert totals["pr_closed_nd"] == 3


def test_firehose_days_length_matches_n_days():
    stats = activity.firehose_stats({}, [], n_days=14, events=[], today=_TODAY, tz=timezone.utc)
    assert len(stats["days"]) == 14
    assert len(stats["pr_incoming"]) == 14
    assert len(stats["pr_closed"]) == 14
    assert len(stats["pr_merged"]) == 14
    assert len(stats["pr_reopened"]) == 14
    assert len(stats["pr_triaged"]) == 14
    assert len(stats["iss_incoming"]) == 14
    assert len(stats["iss_closed"]) == 14


def test_firehose_start_date_overrides_n_days():
    """start_date spans from that date to today, ignoring n_days."""
    from datetime import date
    start = date(2026, 6, 14)   # 10 days before _TODAY (2026-06-24)
    stats = activity.firehose_stats({}, [], n_days=30, events=[], today=_TODAY, tz=timezone.utc, start_date=start)
    assert len(stats["days"]) == 11  # June 14..24 inclusive
    assert stats["days"][0] == "2026-06-14"
    assert stats["days"][-1] == str(_TODAY)


def test_firehose_errored_events_excluded():
    events = [
        _ev(f"{_days_ago(1)}T10:00:00+00:00", "close", pr=1, status="error"),
        _ev(f"{_days_ago(1)}T11:00:00+00:00", "close", pr=2, status="executed"),
    ]
    stats = activity.firehose_stats({}, [], n_days=30, events=events, today=_TODAY, tz=timezone.utc)
    assert sum(stats["pr_triaged"]) == 1  # only the executed one
    assert sum(stats["pr_closed"]) == 1


# ── reopened_after_close ──────────────────────────────────────────────────────

def test_reopened_after_close_detects_reopened_pr():
    # store state='open' after our close means the sweep/INGEST re-observed it open.
    prs = {42: _pr(42, "2026-01-01T00:00:00Z", state="open")}
    events = [_ev("2026-06-20T10:00:00+00:00", "close", pr=42, reason="duplicate")]
    result = activity.reopened_after_close(prs, events)
    assert len(result) == 1
    assert result[0]["pr"] == 42
    assert result[0]["reason"] == "duplicate"


def test_reopened_after_close_ignores_still_closed():
    # our close persisted state='closed'; no reopen observed → not a reopen.
    prs = {10: _pr(10, "2026-01-01T00:00:00Z", state="closed")}
    events = [_ev("2026-06-20T10:00:00+00:00", "close", pr=10)]
    assert activity.reopened_after_close(prs, events) == []


def test_reopened_after_close_trusts_the_live_store_state():
    """The store state is live-maintained (our close writes 'closed'; the sweep
    writes an upstream reopen back to 'open'), so it alone is the reopen signal — a
    closed-by-us PR reads 'closed' until a reopen is actually observed. No stale
    committed 'open' to guard against (the 138-phantom-reopen bug is structural now)."""
    events = [_ev("2026-06-20T10:00:00+00:00", "close", pr=7, reason="stale")]
    assert activity.reopened_after_close(
        {7: _pr(7, "2026-01-01T00:00:00Z", state="closed")}, events) == []   # not yet reopened
    assert [r["pr"] for r in activity.reopened_after_close(
        {7: _pr(7, "2026-01-01T00:00:00Z", state="open")}, events)] == [7]   # reopen observed


def test_reopened_after_close_ignores_merged():
    prs = {20: _pr(20, "2026-01-01T00:00:00Z", state="open")}
    events = [_ev("2026-06-20T10:00:00+00:00", "merge", pr=20)]
    assert activity.reopened_after_close(prs, events) == []


def test_reopened_after_close_latest_action_wins():
    """A PR that was closed then reopened by us shouldn't appear — the latest
    action is reopen, not close."""
    prs = {99: _pr(99, "2026-01-01T00:00:00Z", state="open")}
    events = [
        # newest first
        _ev("2026-06-22T10:00:00+00:00", "reopen", pr=99),
        _ev("2026-06-20T10:00:00+00:00", "close", pr=99),
    ]
    assert activity.reopened_after_close(prs, events) == []


def test_reopened_after_close_results_sorted_newest_first():
    prs = {
        1: _pr(1, "2026-01-01T00:00:00Z", state="open"),
        2: _pr(2, "2026-01-01T00:00:00Z", state="open"),
    }
    events = [
        _ev("2026-06-21T10:00:00+00:00", "close", pr=1),
        _ev("2026-06-22T10:00:00+00:00", "close", pr=2),
    ]
    result = activity.reopened_after_close(prs, events)
    assert [r["pr"] for r in result] == [2, 1]


def test_closed_by_us_prs_are_the_latest_landed_closes():
    events = [
        _ev("2026-06-22T10:00:00+00:00", "reopen", pr=99),   # 99 latest = reopen → excluded
        _ev("2026-06-20T10:00:00+00:00", "close", pr=99),
        _ev("2026-06-21T10:00:00+00:00", "close", pr=42),    # 42 latest = close → included
        _ev("2026-06-21T10:00:00+00:00", "merge", pr=7),     # 7 latest = merge → excluded
    ]
    assert activity.closed_by_us_prs(events) == [42]


# ── pr_author filter ──────────────────────────────────────────────────────────

def test_firehose_pr_author_filters_incoming():
    prs = {
        1: _pr(1, f"{_days_ago(2)}T10:00:00Z", author="upstream-dev"),
        2: _pr(2, f"{_days_ago(2)}T11:00:00Z", author="devin"),
        3: _pr(3, f"{_days_ago(1)}T09:00:00Z", author="upstream-dev"),
    }
    scope = activity.ActivityScope.from_selection(prs, pr_author="upstream-dev")
    stats = activity.firehose_stats(
        scope.prs(prs), [], n_days=30, events=[], today=_TODAY, tz=timezone.utc)
    assert sum(stats["pr_incoming"]) == 2  # only upstream-dev's PRs counted


def test_firehose_pr_author_filters_triage_events():
    prs = {
        10: _pr(10, f"{_days_ago(5)}T10:00:00Z", author="upstream-dev"),
        20: _pr(20, f"{_days_ago(5)}T10:00:00Z", author="devin"),
    }
    events = [
        _ev(f"{_days_ago(3)}T10:00:00+00:00", "merge", pr=10),
        _ev(f"{_days_ago(3)}T11:00:00+00:00", "close", pr=20),
    ]
    scope = activity.ActivityScope.from_selection(prs, pr_author="upstream-dev")
    stats = activity.firehose_stats(
        scope.prs(prs), [], n_days=30, events=scope.events(events),
        today=_TODAY, tz=timezone.utc)
    # Only the merge on pr=10 (upstream-dev's) should be counted; close on pr=20 (devin's) excluded.
    assert sum(stats["pr_merged"]) == 1
    assert sum(stats["pr_closed"]) == 0
    assert sum(stats["pr_triaged"]) == 1


def test_activity_scope_without_selection_returns_all():
    prs = {
        1: _pr(1, f"{_days_ago(2)}T10:00:00Z", author="upstream-dev"),
        2: _pr(2, f"{_days_ago(2)}T10:00:00Z", author="devin"),
    }
    scope = activity.ActivityScope.from_selection(prs)
    stats = activity.firehose_stats(
        scope.prs(prs), [], n_days=30, events=[], today=_TODAY, tz=timezone.utc)
    assert sum(stats["pr_incoming"]) == 2  # both PRs counted with no author filter


def test_author_scope_excludes_issue_and_other_author_events():
    prs = {
        1: _pr(1, f"{_days_ago(2)}T10:00:00Z", author="upstream-dev"),
        2: _pr(2, f"{_days_ago(2)}T10:00:00Z", author="devin"),
    }
    events = [
        _ev(f"{_days_ago(1)}T08:00:00Z", "close", pr=1),
        _ev(f"{_days_ago(1)}T09:00:00Z", "close", pr=2),
        _ev(f"{_days_ago(1)}T10:00:00Z", "issue-close", issue=3),
    ]
    scope = activity.ActivityScope.from_selection(prs, pr_author="upstream-dev")
    assert scope.events(events) == [events[0]]
