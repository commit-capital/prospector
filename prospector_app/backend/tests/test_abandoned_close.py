"""The abandoned-PR lane: which PRs it reads, the comment it posts, the daily
cap, and that it acts on a head once per mode."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline import storekit
from pipeline import store as S
from pipeline.testsupport import greptile_entry, reviews_section
from prospector_app.backend import abandoned_close, data, executor, handback

NOW = datetime.now(timezone.utc)
PUSHED = NOW - timedelta(days=30)
FEEDBACK = PUSHED + timedelta(hours=1)
# conftest stubs the live read for every test.
LIVE_STATE = handback.live_state


def _head(n: int) -> str:
    return f"{n:02d}" + "a" * 38


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setenv("TRIAGE_ABANDONED_CLOSE", "live")
    monkeypatch.setattr(executor, "live_possible", lambda: True)
    monkeypatch.setattr(handback, "live_state", lambda n: _live(n))
    return st


def _live(n: int, **over: object) -> dict:
    return {"state": "open", "head": _head(n), "head_committed": PUSHED, "head_ci": "success",
            "updated_at": FEEDBACK, "author": f"dev{n}", "last_active": PUSHED, **over}


@pytest.fixture
def closes(monkeypatch):
    out: list[tuple[int, str | None, bool]] = []
    monkeypatch.setattr(executor, "execute_pr", lambda n, action, *, dry_run: out.append(
        (n, action.comment, dry_run)) or {"status": "dry-run" if dry_run else "executed"})
    return out


def _next_pass(after: datetime | None = None) -> datetime:
    """A time past the interval since the last pass, which was booked at the real now."""
    return (after or datetime.now(timezone.utc)) + timedelta(
        seconds=abandoned_close.INTERVAL_SECONDS + 1)


def _save(st: S.Store, n: int, *, score: int = 3, at: datetime = FEEDBACK,
          **meta: object) -> None:
    head = _head(n)
    greptile = {**greptile_entry(score, head), "observed_at": at.isoformat()}
    st.save_pr({"pr": n, "meta": {"title": f"pr {n}", "state": "open", "head_sha": head,
                                  "author": f"dev{n}", "author_association": "CONTRIBUTOR",
                                  **meta},
                "reviews": reviews_section(head, greptile=greptile),
                "signals": {"ci": "passing", "against_head_sha": head,
                            "checked_at": storekit.now()}})
    data.refresh()


def test_candidates_need_a_contributor_and_feedback_at_the_head_two_weeks_old(store):
    _save(store, 1)
    _save(store, 5, author_association="OWNER")
    _save(store, 2, score=5)
    _save(store, 3, at=NOW - timedelta(days=3))
    _save(store, 4, state="closed")
    assert [pr.number for pr in abandoned_close.candidates(data.prs(), NOW)] == [1]


def test_the_dry_run_command_writes_nothing(store, closes, capsys):
    _save(store, 1)
    _save(store, 2, author_association="MEMBER")
    assert abandoned_close.main([]) == 0
    out = capsys.readouterr().out
    assert "#1      close @dev1  silent since" in out
    assert "#2" not in out
    assert '"close": 1' in out
    assert closes == [] and store.runs(phase=abandoned_close.PHASE) == []


def test_off_does_nothing(store, closes, monkeypatch):
    monkeypatch.delenv("TRIAGE_ABANDONED_CLOSE")
    _save(store, 1)
    assert abandoned_close.sweep_once() is None
    assert closes == []


def test_a_pass_closes_within_the_daily_cap_and_each_head_once(store, closes, monkeypatch):
    monkeypatch.setenv("TRIAGE_ABANDONED_CLOSE_DAILY", "2")
    for n in (1, 2, 3):
        _save(store, n, at=FEEDBACK + timedelta(minutes=n))
    assert abandoned_close.sweep_once(NOW) == [1, 2]
    assert [(n, dry) for n, _, dry in closes] == [(1, False), (2, False)]
    # The day's cap is spent, and a later pass waits out the interval.
    later = _next_pass()
    assert abandoned_close.sweep_once(later) == []
    # A PR reopened at the head it was closed at is not closed again.
    monkeypatch.setenv("TRIAGE_ABANDONED_CLOSE_DAILY", "10")
    assert abandoned_close.sweep_once(_next_pass(later)) == [3]
    steps = [r.raw["stats"]["step"] for r in store.runs(phase=abandoned_close.PHASE)]
    assert steps == ["closed", "closed", "closed"]


def test_a_dry_run_head_still_closes_once_the_lane_goes_live(store, closes, monkeypatch):
    monkeypatch.setenv("TRIAGE_ABANDONED_CLOSE", "dry-run")
    _save(store, 1)
    assert abandoned_close.sweep_once(NOW) == [1]
    monkeypatch.setenv("TRIAGE_ABANDONED_CLOSE", "live")
    assert abandoned_close.sweep_once(_next_pass()) == [1]
    assert [dry for *_, dry in closes] == [True, False]


def test_a_push_seen_live_or_an_inflight_fix_keeps_the_pr_open(store, closes, monkeypatch):
    _save(store, 1)
    _save(store, 2)
    store.edit_pr(2).record_fix_request("queued", "rebase", head_sha=_head(2))
    data.refresh()
    monkeypatch.setattr(handback, "live_state", lambda n: _live(n, head="f" * 40))
    assert abandoned_close.judge(data.prs()[1], NOW)[:2] == (
        False, "its live head is not the ingested one")
    assert abandoned_close.judge(data.prs()[2], NOW)[1] == "its rebase request is queued"
    assert abandoned_close.sweep_once(NOW) == []
    assert closes == []


def test_a_live_pass_needs_the_bots_token(store, closes, monkeypatch):
    monkeypatch.setattr(executor, "live_possible", lambda: False)
    _save(store, 1)
    assert abandoned_close.sweep_once(NOW) is None
    assert closes == []


def test_a_live_close_that_fell_back_to_a_dry_run_is_not_booked(store, monkeypatch):
    monkeypatch.setattr(executor, "execute_pr",
                        lambda n, action, *, dry_run: {"status": "dry-run", "forced": True})
    _save(store, 1)
    assert abandoned_close.sweep_once(NOW) == []
    assert store.runs(phase=abandoned_close.PHASE) == []


def test_the_close_comment_names_the_feedback_and_invites_the_author_back(store, closes):
    _save(store, 1)
    abandoned_close.sweep_once(NOW)
    body = closes[0][1] or ""
    assert body.startswith("Hi @dev1 — we're closing this PR for now")
    assert "- Greptile scored it 3/5" in body
    assert "comment here and we'll reopen it" in body
    assert body.endswith(abandoned_close.marker(_head(1)))


def test_live_state_reads_the_head_and_the_authors_own_edit(monkeypatch):
    def commit(day: int, who: str, ci: str | None = None) -> dict:
        return {"commit": {"committedDate": f"2026-01-0{day}T00:00:00Z",
                           "statusCheckRollup": {"state": ci} if ci else None,
                           "authors": {"nodes": [{"user": {"login": who}}]}}}

    def pr(editor: str) -> dict:
        return {"repository": {"pullRequest": {
            "state": "OPEN", "headRefOid": "h", "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-06T00:00:00Z", "lastEditedAt": "2026-01-05T00:00:00Z",
            "author": {"login": "dev"}, "editor": {"login": editor},
            "commits": {"nodes": [commit(2, "dev"), commit(3, "bot", "FAILURE")]},
            "comments": {"nodes": []}, "reviews": {"nodes": []}}}}

    monkeypatch.setattr("pipeline.gh.gh_graphql_data", lambda q, timeout: pr("dev"))
    live = LIVE_STATE(1)
    assert live is not None
    assert live["head_committed"] == datetime(2026, 1, 3, tzinfo=timezone.utc)
    assert (live["head_ci"], live["updated_at"]) == (
        "failure", datetime(2026, 1, 6, tzinfo=timezone.utc))
    assert live["last_active"] == datetime(2026, 1, 5, tzinfo=timezone.utc)
    monkeypatch.setattr("pipeline.gh.gh_graphql_data", lambda q, timeout: pr("maintainer"))
    assert (LIVE_STATE(1) or {})["last_active"] == datetime(2026, 1, 2, tzinfo=timezone.utc)
