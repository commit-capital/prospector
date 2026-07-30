"""Backlog progress (#27): progress() pairs landed live actions from the log
against the open-PR count from the store. Pure; inputs passed directly."""
from app.backend import activity
from pipeline.model import Pr


def _pr(n: int, state: str) -> Pr:
    return Pr(None, {"pr": n, "meta": {"title": f"t{n}", "author": "a",
                                       "state": state, "head_sha": "h"}})


def store(states):
    return {n: _pr(n, s) for n, s in states.items()}


def ev(at, kind, pr, **extra):
    return {"at": at, "kind": kind, "pr": pr, "status": "executed", "dry_run": False, **extra}


def test_counts_landed_actions_against_open_total():
    prs = store({1: "open", 2: "open", 3: "open", 4: "open"})
    events = [
        ev("2026-06-10T12:00:00", "merge", 1, status="merged"),
        ev("2026-06-10T11:00:00", "close", 2, reason="duplicate"),
        ev("2026-06-10T10:00:00", "close", 3, reason="stale"),
    ]
    p = activity.progress(prs, events)
    assert p["open_total"] == 4 and p["merged"] == 1 and p["closed"] == 2
    assert p["actioned"] == 3 and p["remaining"] == 1
    assert p["by_reason"] == {"duplicate": 1, "stale": 1}
    assert p["pct"] == 75.0


def test_dry_runs_do_not_count():
    prs = store({1: "open", 2: "open"})
    events = [{"at": "2026-06-10T10:00:00", "kind": "close", "pr": 1, "dry_run": True, "status": "dry-run"}]
    p = activity.progress(prs, events)
    assert p["actioned"] == 0 and p["remaining"] == 2


def test_reopen_returns_pr_to_backlog():
    prs = store({1: "open"})
    events = [  # newest-first: reopen is the latest action on #1
        ev("2026-06-10T11:00:00", "reopen", 1, status="reopened"),
        ev("2026-06-10T10:00:00", "close", 1),
    ]
    p = activity.progress(prs, events)
    assert p["actioned"] == 0 and p["closed"] == 0 and p["remaining"] == 1


def test_actioned_prs_that_left_the_open_set_still_count_toward_the_universe():
    # Prod reality: acting on a PR flips its store state out of "open", so the done
    # set and the still-open set are disjoint. The universe (denominator) is their
    # union — it must not shrink as we work, or progress would run away from 100%.
    prs = store({1: "open", 2: "open", 3: "closed", 4: "merged"})
    events = [
        ev("2026-06-10T12:00:00", "merge", 4, status="merged"),
        ev("2026-06-10T11:00:00", "close", 3, reason="stale"),
    ]
    p = activity.progress(prs, events)
    assert p["open_total"] == 2      # only the still-open #1, #2
    assert p["actioned"] == 2        # #3, #4 — we did them, they left "open"
    assert p["universe"] == 4        # 2 open + 2 done, disjoint
    assert p["remaining"] == 2       # the still-open ones are what's left
    assert p["pct"] == 50.0          # 2 of 4, not 2/2


def test_universe_dedupes_an_unreconciled_actioned_pr():
    # If a just-closed PR's state write-back hasn't landed yet it can be both "open"
    # and actioned; the union must count it once.
    prs = store({1: "open", 2: "open"})
    events = [ev("2026-06-10T10:00:00", "close", 1, reason="stale")]
    p = activity.progress(prs, events)
    assert p["universe"] == 2 and p["actioned"] == 1 and p["remaining"] == 1


def test_only_open_prs_form_the_backlog():
    prs = store({1: "open", 2: "closed", 3: "merged"})
    p = activity.progress(prs, [])
    assert p["open_total"] == 1 and p["universe"] == 1


def test_empty_store_is_safe():
    p = activity.progress({}, [])
    assert p["open_total"] == 0 and p["pct"] == 0.0 and p["remaining"] == 0
