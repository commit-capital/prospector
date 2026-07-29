"""Activity metrics aggregation (#42): summarize() folds events into totals +
buckets grouped by day/week/cluster/identity. Pure; events passed directly."""
from datetime import timedelta, timezone
from review_cockpit.backend import activity


def ev(at, kind, **extra):
    return {"at": at, "kind": kind, "status": "executed", "dry_run": False, **extra}


EVENTS = [
    ev("2026-06-10T09:00:00", "close", cluster_id="001", identity="test-bot", reason="duplicate"),
    ev("2026-06-10T11:00:00", "close", cluster_id="001", identity="test-bot"),
    ev("2026-06-10T12:00:00", "merge", cluster_id="002", identity="test-bot"),
    ev("2026-06-11T09:00:00", "comment", cluster_id="002", identity="test-user"),
    {"at": "2026-06-11T10:00:00", "kind": "close", "dry_run": True, "cluster_id": "003"},  # dry-run
]


def test_totals_exclude_dry_run_by_default():
    s = activity.summarize(EVENTS, group_by="day")
    assert s["totals"]["close"] == 2 and s["totals"]["merge"] == 1 and s["totals"]["comment"] == 1


def test_include_dry_run_opt_in():
    s = activity.summarize(EVENTS, group_by="day", include_dry_run=True)
    assert s["totals"]["close"] == 3


def test_group_by_day_buckets_sorted_chronologically():
    s = activity.summarize(EVENTS, group_by="day", tz=timezone.utc)
    keys = [b["key"] for b in s["buckets"]]
    assert keys == ["2026-06-10", "2026-06-11"]
    assert s["buckets"][0]["close"] == 2 and s["buckets"][0]["merge"] == 1 and s["buckets"][0]["total"] == 3


def test_group_by_cluster_sorted_by_volume():
    s = activity.summarize(EVENTS, group_by="cluster")
    # cluster 001 has 2 events, 002 has 2 — tie broken by key asc; both before none
    keys = [b["key"] for b in s["buckets"]]
    assert keys[0] in ("001", "002") and "003" not in keys  # 003 was dry-run only


def test_group_by_identity():
    s = activity.summarize(EVENTS, group_by="identity")
    by = {b["key"]: b["total"] for b in s["buckets"]}
    assert by["test-bot"] == 3 and by["test-user"] == 1


def test_group_by_week():
    s = activity.summarize(EVENTS, group_by="week", tz=timezone.utc)
    assert all(k["key"].startswith("2026-W") for k in s["buckets"])


def test_since_until_bounds_inclusive():
    s = activity.summarize(EVENTS, group_by="day", since="2026-06-11", until="2026-06-11", tz=timezone.utc)
    keys = [b["key"] for b in s["buckets"]]
    assert keys == ["2026-06-11"] and s["totals"]["comment"] == 1 and s["totals"]["close"] == 0


def test_summarize_buckets_by_operator_local_day_not_utc():
    """A landed action stamped just after UTC midnight buckets on the operator's
    local day, so the per-day actions chart shows it where the operator saw it —
    matching firehose_stats and the local-time timestamp the cockpit renders."""
    pacific = timezone(timedelta(hours=-8))
    # 00:58 UTC on the 26th is 16:58 on the 25th in Pacific.
    events = [ev("2026-06-26T00:58:43+00:00", "merge", pr=3800, status="merged")]
    s = activity.summarize(events, group_by="day", tz=pacific)
    assert [b["key"] for b in s["buckets"]] == ["2026-06-25"]
    # since/until bound on the same local day, not the UTC day.
    bounded = activity.summarize(events, group_by="day",
                                 since="2026-06-25", until="2026-06-25", tz=pacific)
    assert bounded["totals"]["merge"] == 1


def test_bad_group_by_falls_back_to_day():
    s = activity.summarize(EVENTS, group_by="bogus")
    assert s["group_by"] == "day"


def test_identity_filter():
    s = activity.summarize(EVENTS, group_by="day", identity="test-user")
    assert s["totals"]["comment"] == 1 and s["totals"]["close"] == 0 and s["totals"]["merge"] == 0


def test_errored_attempt_not_counted():
    """A merge that errors then succeeds on retry is one landed action, not two
    (the #2619 regression: summarize used to tally the errored attempt too)."""
    events = [
        ev("2026-06-15T14:33:55", "merge", pr=2619, status="merged"),
        ev("2026-06-15T14:23:09", "merge", pr=2619, status="error"),
    ]
    s = activity.summarize(events, group_by="day")
    assert s["totals"]["merge"] == 1


def test_skipped_attempt_not_counted():
    events = [ev("2026-06-15T10:00:00", "close", status="skipped")]
    assert activity.summarize(events, group_by="day")["totals"]["close"] == 0
