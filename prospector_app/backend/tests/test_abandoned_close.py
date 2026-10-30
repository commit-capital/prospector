"""The abandoned-PR lane: which PRs it reads, the comment it posts, the daily
cap, and that it acts on a head once per mode."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline import settings, storekit
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
    assert "#1      close  @dev1  silent since" in out
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
    assert abandoned_close.judge(data.prs()[1], NOW).why == "its live head is not the ingested one"
    assert abandoned_close.judge(data.prs()[2], NOW).why == "its rebase request is queued"
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


def _save_ci(st: S.Store, n: int, *, score: int = 5, pushed_by_us: str | None = None) -> None:
    """PR `n` with CI failing on its head an hour after the push."""
    _save(st, n, score=score)
    rec = st.edit_pr(n)
    rec.raw["signals"].update(ci="failing", ci_failures={
        "failures": [{"name": "verify", "started_at": FEEDBACK.isoformat()}]})
    st.save_pr(rec.raw)
    if pushed_by_us:
        st.edit_pr(n).record_fix_request("pushed", pushed_by_us, head_sha=_head(n),
                                         result={"pushed_head_sha": _head(n)})
    data.refresh()


@pytest.fixture
def failing_ci(store, monkeypatch):
    monkeypatch.setattr(handback, "live_state", lambda n: _live(n, head_ci="failure"))


@pytest.fixture
def drift(monkeypatch):
    """Local git's view of every PR: behind the base, rebasing cleanly unless
    a test says otherwise."""
    from prospector_app.backend import backlog_sweep
    probe = backlog_sweep.Probe(head_date=PUSHED.isoformat(), current=False,
                                merge_commits=False, rebase_clean=True, conflicts=[],
                                changed=["src/a.ts"])
    monkeypatch.setattr(backlog_sweep, "fetch", lambda prs: None)
    monkeypatch.setattr(backlog_sweep, "probe", lambda n, head: probe)
    return probe


@pytest.fixture
def queued(monkeypatch):
    from prospector_app.backend import fix_queue
    out: list[tuple[int, str]] = []

    def queue(n: int, action: str) -> dict:
        out.append((n, action))
        data.store().edit_pr(n).record_fix_request("queued", action, head_sha=_head(n))
        data.refresh()
        return {}
    monkeypatch.setattr(fix_queue, "queue_pr", queue)
    return out


@pytest.fixture
def asks(monkeypatch):
    out: list[int] = []
    monkeypatch.setattr(executor, "hand_back_comment", lambda n, body, *, marker, dry_run: (
        out.append(n) or {"status": "executed", "event_url": f"https://x/{n}"}))
    return out


def _end(st: S.Store, n: int, action: str, status: str, **result: object) -> None:
    st.edit_pr(n).record_fix_request(status, action, head_sha=_head(n), result=result)
    data.refresh()


def test_ci_on_an_old_base_is_brought_current_before_it_can_close(
        store, failing_ci, drift, closes, queued):
    _save_ci(store, 1)
    assert abandoned_close.judge(data.prs()[1], NOW).why == abandoned_close.gates.STALE_CI
    assert abandoned_close.sweep_once(NOW) == [1]
    assert queued == [(1, "rebase")] and closes == []
    assert [r.raw["stats"]["step"] for r in store.runs(phase=abandoned_close.PHASE)] == ["queued"]


def test_ci_on_a_head_we_brought_current_or_that_carries_the_base_closes(
        store, failing_ci, drift, closes, queued):
    _save_ci(store, 1, pushed_by_us="update")
    _save_ci(store, 2)
    drift_current = abandoned_close.backlog_sweep.Probe(**{**drift.__dict__, "current": True})
    abandoned_close.backlog_sweep.probe = lambda n, head: drift_current if n == 2 else drift
    assert abandoned_close.judge(data.prs()[1], NOW).close
    assert abandoned_close.sweep_once(NOW) == [1, 2]
    assert [n for n, *_ in closes] == [1, 2] and queued == []
    assert "- CI failed (verify)" in (closes[0][1] or "")


def test_other_feedback_closes_without_naming_ci_on_an_old_base(store, failing_ci, closes):
    _save_ci(store, 1, score=3)
    v = abandoned_close.judge(data.prs()[1], NOW)
    assert v.close and v.feedback == ["Greptile scored it 3/5"]


def test_a_dry_run_only_books_what_it_would_queue(store, failing_ci, drift, closes, queued,
                                                    monkeypatch):
    monkeypatch.setenv("TRIAGE_ABANDONED_CLOSE", "dry-run")
    _save_ci(store, 1)
    assert abandoned_close.sweep_once(NOW) == [1]
    assert queued == []
    [run] = store.runs(phase=abandoned_close.PHASE)
    assert run.raw["stats"]["step"] == "dry-run"
    assert run.raw["stats"]["why"].startswith("would queue rebase")


def test_a_drift_no_agent_can_resolve_gets_our_rebase_ask_which_then_closes(
        store, failing_ci, drift, closes, queued, asks, monkeypatch):
    drift.rebase_clean, drift.conflicts = False, [f"f{i}.ts" for i in range(9)]
    _save_ci(store, 1)
    assert abandoned_close.sweep_once(NOW) == [1]
    assert asks == [1] and queued == []
    data.refresh()
    ask = data.prs()[1].section("rebase_ask") or {}
    assert ask["head_sha"] == _head(1) and ask["url"] == "https://x/1"
    # The ask is blocking feedback: two weeks of the author's silence closes.
    later = datetime.now(timezone.utc) + timedelta(days=15)
    monkeypatch.setattr(handback, "live_state",
                        lambda n: _live(n, head_ci="failure", updated_at=NOW))
    v = abandoned_close.judge(data.prs()[1], later)
    assert v.close and v.feedback == [f"we asked for a rebase onto `{settings.default_branch()}`"]
    # An answer from the author keeps it open.
    monkeypatch.setattr(handback, "live_state", lambda n: _live(
        n, head_ci="failure", updated_at=NOW, last_active=NOW + timedelta(hours=1)))
    assert not abandoned_close.judge(data.prs()[1], later).close


def test_a_request_that_hit_conflicts_asks_and_one_the_worker_ended_requeues_once(
        store, failing_ci, drift, closes, queued, asks):
    _save_ci(store, 1)
    _save_ci(store, 2)
    assert abandoned_close.sweep_once(NOW) == [1, 2]
    _end(store, 1, "rebase", "refused", conflict_paths=["a.ts"])
    _end(store, 2, "rebase", "failed")
    later = _next_pass()
    assert abandoned_close.sweep_once(later) == [1, 2]
    assert asks == [1] and queued == [(1, "rebase"), (2, "rebase"), (2, "rebase")]
    # The worker ended it again: the author is asked.
    _end(store, 2, "rebase", "failed")
    assert abandoned_close.sweep_once(_next_pass(later)) == [2]
    assert asks == [1, 2] and len(queued) == 3
    steps = [r.raw["stats"]["step"] for r in store.runs(phase=abandoned_close.PHASE)]
    assert sorted(steps) == ["asked", "asked", "queued", "queued", "requeued"]


def test_a_branch_the_bot_may_not_touch_gets_the_ask(store, failing_ci, drift, closes, queued,
                                                    asks, monkeypatch):
    monkeypatch.setattr(abandoned_close.gates, "fix_eligibility",
                        lambda *a, **k: (False, "maintainers cannot modify it"))
    _save_ci(store, 1)
    assert abandoned_close.sweep_once(NOW) == [1]
    assert asks == [1] and queued == []
