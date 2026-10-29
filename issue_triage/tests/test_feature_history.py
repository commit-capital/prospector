from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_triage import feature_history
from issue_triage.issue_store import IssueStore
from pipeline import gh, settings


@pytest.fixture(autouse=True)
def _scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(settings, "repo", lambda: "o/r")
    monkeypatch.setattr(gh, "issue_comments", lambda n, **kw: [])


def _raw(n: int, **kw: object) -> dict:
    return {"number": n, "title": f"t{n}", "state_reason": "completed", "labels": [], "body": "b", **kw}


def test_render_closed_keeps_only_maintainer_comments() -> None:
    raw = _raw(12, title="Dark mode", state_reason="not_planned", labels=[{"name": "enhancement"}],
               body="please")
    comments = [{"author_association": "MEMBER", "user": {"login": "m"},
                 "body": "Not for us: theming is out of scope"},
                {"author_association": "NONE", "user": {"login": "u"}, "body": "+1"}]
    text = feature_history.render_closed(raw, comments)
    assert "not_planned" in text and "theming is out of scope" in text and "+1" not in text


def test_render_closed_truncates() -> None:
    text = feature_history.render_closed(
        _raw(1, body="x" * 5000),
        [{"author_association": "OWNER", "user": {"login": "m"}, "body": "z" * 5000}])
    assert text.count("x") == 2000 and text.count("z") == 1500


def test_refresh_is_fresh_for_a_day(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(gh, "gh_list", lambda path, **kw: calls.append(path) or [_raw(5)])
    d = feature_history.refresh([])
    feature_history.refresh([])
    assert len(calls) == 1 and "labels=enhancement" in calls[0] and "state=closed" in calls[0]
    assert (d / "closed-5.md").exists()
    feature_history.refresh([], force=True)
    assert len(calls) == 2


def test_refresh_quotes_the_label(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(feature_history.profile, "active",
                        lambda: SimpleNamespace(feature_labels=("type: feature & idea",)))
    monkeypatch.setattr(gh, "gh_list", lambda path, **kw: calls.append(path) or [])
    feature_history.refresh([])
    assert calls[0].endswith("labels=type%3A%20feature%20%26%20idea&sort=updated&per_page=100")


def test_refresh_drops_pull_requests_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_raw(i, updated_at=f"2026-01-{i % 28 + 1:02d}") for i in range(1, 200)]
    items.append(_raw(999, pull_request={}))
    monkeypatch.setattr(gh, "gh_list", lambda path, **kw: items)
    d = feature_history.refresh([])
    assert not (d / "closed-999.md").exists()
    assert len(list(d.glob("closed-*.md"))) == feature_history.HISTORY_MAX


def test_refresh_keeps_old_files_when_fetch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gh, "gh_list", lambda path, **kw: [_raw(5)])
    d = feature_history.refresh([])
    monkeypatch.setattr(gh, "gh_list", lambda path, **kw: None)
    feature_history.refresh([], force=True)
    assert (d / "closed-5.md").exists()


def _expire(d: Path) -> None:
    os.utime(d / ".fetched", (0, 0))


def test_a_refresh_with_nothing_changed_lists_only(monkeypatch: pytest.MonkeyPatch) -> None:
    lists: list[str] = []
    comments: list[int] = []
    rows = [_raw(5, updated_at="2026-01-05T00:00:00Z"), _raw(6, updated_at="2026-01-06T00:00:00Z")]
    monkeypatch.setattr(gh, "gh_list", lambda path, **kw: lists.append(path) or rows)
    monkeypatch.setattr(gh, "issue_comments", lambda n, **kw: comments.append(n) or [])
    d = feature_history.refresh([])
    assert sorted(comments) == [5, 6] and "since=" not in lists[0]
    _expire(d)
    rows = [rows[1]]
    feature_history.refresh([])
    assert sorted(comments) == [5, 6] and len(lists) == 2
    assert lists[1].endswith("&since=2026-01-06T00%3A00%3A00Z")
    assert (d / "closed-5.md").exists() and (d / "closed-6.md").exists()


def test_a_changed_issue_refetches_its_comments(monkeypatch: pytest.MonkeyPatch) -> None:
    comments: list[int] = []
    rows = [_raw(5, updated_at="2026-01-05T00:00:00Z")]
    monkeypatch.setattr(gh, "gh_list", lambda path, **kw: rows)
    monkeypatch.setattr(gh, "issue_comments", lambda n, **kw: comments.append(n) or [
        {"author_association": "OWNER", "user": {"login": "m"}, "body": f"call {len(comments)}"}])
    d = feature_history.refresh([])
    _expire(d)
    rows = [_raw(5, updated_at="2026-01-09T00:00:00Z")]
    feature_history.refresh([])
    assert comments == [5, 5] and "call 2" in (d / "closed-5.md").read_text()


def test_an_incremental_refresh_keeps_the_newest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feature_history, "HISTORY_MAX", 2)
    rows = [_raw(5, updated_at="2026-01-05"), _raw(6, updated_at="2026-01-06")]
    monkeypatch.setattr(gh, "gh_list", lambda path, **kw: rows)
    d = feature_history.refresh([])
    _expire(d)
    rows = [_raw(7, updated_at="2026-01-07")]
    feature_history.refresh([])
    assert sorted(p.name for p in d.glob("closed-*.md")) == ["closed-6.md", "closed-7.md"]


def _decided(store: IssueStore, n: int, at: str) -> None:
    store.save_issue({"issue": n, "meta": {"title": f"want {n}", "state": "open", "body": "",
                                           "updated_at": "t", "content_updated_at": "t"}})
    store.edit_issue(n).record_feature_decision({"decision": "decline", "by": "m", "at": at})


def test_render_decisions_keeps_the_newest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(feature_history, "HISTORY_MAX", 2)
    store = IssueStore(tmp_path)
    for n, at in ((3, "2026-01-03"), (4, "2026-01-04"), (5, "2026-01-05")):
        _decided(store, n, at)
    text = feature_history.render_decisions(store.load_issue(n) for n in (3, 4, 5))
    assert [line.split()[0] for line in text.splitlines()[2:]] == ["#5", "#4"]


def test_decisions_are_rewritten_only_when_they_change(monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path) -> None:
    monkeypatch.setattr(gh, "gh_list", lambda path, **kw: [])
    store = IssueStore(tmp_path)
    _decided(store, 3, "2026-01-03")
    d = feature_history.refresh([store.load_issue(3)])
    os.utime(d / "decisions.md", (0, 0))
    feature_history.refresh([store.load_issue(3)])
    assert (d / "decisions.md").stat().st_mtime == 0
    _decided(store, 4, "2026-01-04")
    feature_history.refresh([store.load_issue(n) for n in (3, 4)])
    assert "#4" in (d / "decisions.md").read_text()


def test_render_decisions_lists_declines_and_implements(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    for n, dec in ((3, "decline"), (4, "implement")):
        store.save_issue({"issue": n, "meta": {"title": f"want {n}", "state": "open", "body": "",
                                               "updated_at": "t", "content_updated_at": "t"}})
        store.edit_issue(n).record_feature_decision(
            {"decision": dec, "by": "m", "at": "t", "guidance": "keep it small"})
    store.save_issue({"issue": 5, "meta": {"title": "undecided", "state": "open", "body": "",
                                           "updated_at": "t", "content_updated_at": "t"}})
    text = feature_history.render_decisions(store.load_issue(n) for n in (3, 4, 5))
    assert "#3" in text and "decline" in text and "#4" in text and "implement" in text
    assert "keep it small" in text and "undecided" not in text


def test_precedents_include_closed_files_and_decisions(monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path) -> None:
    monkeypatch.setattr(gh, "gh_list", lambda path, **kw: [_raw(5)])
    store = IssueStore(tmp_path)
    store.save_issue({"issue": 3, "meta": {"title": "w", "state": "open", "body": "",
                                           "updated_at": "t", "content_updated_at": "t"}})
    store.edit_issue(3).record_feature_decision({"decision": "decline", "by": "m", "at": "t"})
    feature_history.refresh([store.load_issue(3)])
    assert feature_history.precedents() == {3, 5}
