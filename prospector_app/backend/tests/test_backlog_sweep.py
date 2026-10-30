"""The backlog sweep: its population, its local-git classification, and the
live run's queueing, asking, limit and once-per-head bound."""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import settings
from pipeline import store as S
from pipeline.testsupport import greptile_entry, reviews_section
from prospector_app.backend import backlog_sweep, data, executor

OLD = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
RECENT = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()


def _git(repo: Path, *args: str, date: str = OLD) -> str:
    env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
           "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a",
           "GIT_COMMITTER_EMAIL": "a@x"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def _commit(repo: Path, files: dict[str, str], msg: str, date: str = OLD) -> str:
    for name, text in files.items():
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg, date=date)
    return _git(repo, "rev-parse", "HEAD")


class Repo:
    """A local repository standing in for the fetched base and PR heads."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir()
        _git(path, "init", "-q", "-b", settings.default_branch())
        self.root = _commit(path, {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"}, "root")

    def pr(self, n: int, files: dict[str, str], *, date: str = OLD) -> str:
        _git(self.path, "checkout", "-q", "-b", f"pr{n}", self.root)
        head = _commit(self.path, files, f"pr {n}", date=date)
        _git(self.path, "update-ref", f"refs/pr/{n}", head)
        _git(self.path, "checkout", "-q", settings.default_branch())
        return head

    def advance_base(self, files: dict[str, str]) -> None:
        _commit(self.path, files, "base moves")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = Repo(tmp_path / "repo")
    monkeypatch.setattr(backlog_sweep, "REPO_DIR", r.path)
    monkeypatch.setattr(backlog_sweep, "fetch", lambda prs: None)
    return r


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    return st


def _save(st: S.Store, n: int, head: str, greptile: dict | None = None, **meta: object) -> None:
    st.save_pr({"pr": n, "meta": {"title": f"pr {n}", "state": "open", "head_sha": head,
                                  "author": f"dev{n}", "updated_at": OLD,
                                  "head_repo": "fork/test-repo", "maintainer_can_modify": True,
                                  **meta},
                "reviews": reviews_section(head, **({"greptile": greptile} if greptile else {})),
                "signals": {"ci": "passing"}})


def _plans() -> dict[int, backlog_sweep.Plan]:
    data.refresh()
    return {p.pr: p for p in backlog_sweep.plans()}


def test_population_needs_quiet_and_an_author_done(store, repo):
    _save(store, 1, repo.pr(1, {"d.txt": "1\n"}))
    _save(store, 2, repo.pr(2, {"d.txt": "2\n"}), updated_at=RECENT)
    head = repo.pr(3, {"d.txt": "3\n"})
    _save(store, 3, head, greptile=greptile_entry(3, head))
    _save(store, 4, repo.pr(4, {"d.txt": "4\n"}), greptile=greptile_entry(2, "f" * 40))
    plans = _plans()
    assert set(plans) == {1, 4}
    assert plans[1].kind == "greptile-bar" and plans[4].kind == "unreviewed-push"


def test_classifies_clean_resolvable_author_and_ineligible(store, repo):
    clean = repo.pr(1, {"d.txt": "new\n"})
    small = repo.pr(2, {"a.txt": "mine\n"})
    many = repo.pr(3, {f"f{i}.txt": "mine\n" for i in range(7)} | {"a.txt": "x\n"})
    locked = repo.pr(4, {"a.txt": "theirs\n"})
    repo.advance_base({"a.txt": "base\n"} | {f"f{i}.txt": "base\n" for i in range(7)})
    for n, head in ((1, clean), (2, small), (3, many)):
        _save(store, n, head)
    _save(store, 4, locked, maintainer_can_modify=False)
    plans = _plans()
    assert (plans[1].bucket, plans[1].action) == ("clean", "rebase")
    assert (plans[2].bucket, plans[2].action, plans[2].conflicts) == ("resolvable", "rebase",
                                                                       ["a.txt"])
    assert plans[3].bucket == "author" and plans[3].ask and len(plans[3].conflicts) == 8
    assert plans[4].bucket == "ineligible" and "Allow edits" in plans[4].why


def test_a_recent_head_commit_or_moved_head_is_skipped(store, repo):
    _save(store, 1, repo.pr(1, {"d.txt": "1\n"}, date=RECENT))
    repo.pr(2, {"d.txt": "2\n"})
    _save(store, 2, "e" * 40)
    plans = _plans()
    assert plans[1].bucket == "skip" and "recent" in plans[1].why
    assert plans[2].bucket == "skip" and "moved" in plans[2].why


def test_dry_run_writes_nothing(store, repo, capsys):
    _save(store, 1, repo.pr(1, {"d.txt": "1\n"}))
    repo.advance_base({"z.txt": "z\n"})
    assert backlog_sweep.main([]) == 0
    assert '"clean": 1' in capsys.readouterr().out
    assert store.load_pr(1).fix_request is None
    assert store.runs(phase=backlog_sweep.PHASE) == []


def test_live_needs_a_limit(store, repo):
    _save(store, 1, repo.pr(1, {"d.txt": "1\n"}))
    repo.advance_base({"z.txt": "z\n"})
    with pytest.raises(SystemExit):
        backlog_sweep.main(["--live"])
    assert store.load_pr(1).fix_request is None


def test_live_queues_then_asks_once_and_honours_the_limit(store, repo, monkeypatch):
    asks: list[tuple[int, str]] = []
    monkeypatch.setattr(executor, "hand_back_comment",
                        lambda n, body, *, marker, dry_run: asks.append((n, body)) or
                        {"status": "executed"})
    heads = {1: repo.pr(1, {"a.txt": "one\n"}),
             2: repo.pr(2, {f"f{i}.txt": "mine\n" for i in range(7)}),
             3: repo.pr(3, {"d.txt": "3\n"})}
    repo.advance_base({"a.txt": "base\n"} | {f"f{i}.txt": "base\n" for i in range(7)})
    for n, head in heads.items():
        _save(store, n, head, updated_at=(datetime.now(timezone.utc)
                                          - timedelta(days=60 + n)).isoformat())
    backlog_sweep.main(["--live", "--limit", "2"])
    assert store.load_pr(1).fix_request["action"] == "rebase"
    assert [n for n, _ in asks] == [2]
    assert "rebase it onto the latest" in asks[0][1]
    assert store.load_pr(3).fix_request is None  # the limit of 2 is spent

    # The queued resolve ends on its conflicts: the next run asks.
    rec = store.edit_pr(1)
    rec.record_fix_request("refused", "rebase", head_sha=heads[1],
                           result={"conflict_paths": ["a.txt"]})
    data.refresh()
    backlog_sweep.main(["--live", "--limit", "4"])
    assert [n for n, _ in asks] == [2, 1]
    assert store.load_pr(3).fix_request["action"] == "rebase"

    # Every head was acted on once each way; another run does nothing.
    data.refresh()
    before = len(store.runs(phase=backlog_sweep.PHASE))
    backlog_sweep.main(["--live", "--limit", "50"])
    assert len(store.runs(phase=backlog_sweep.PHASE)) == before
    assert [n for n, _ in asks] == [2, 1]


def test_an_ask_already_on_the_pr_is_recorded_without_spending_the_limit(store, repo, monkeypatch):
    monkeypatch.setattr(executor, "hand_back_comment",
                        lambda n, body, *, marker, dry_run: {"status": "exists"})
    _save(store, 1, repo.pr(1, {f"f{i}.txt": "1\n" for i in range(7)}), updated_at=(
          datetime.now(timezone.utc) - timedelta(days=61)).isoformat())
    _save(store, 2, repo.pr(2, {"d.txt": "2\n"}))
    repo.advance_base({f"f{i}.txt": "base\n" for i in range(7)} | {"z.txt": "z\n"})
    backlog_sweep.main(["--live", "--limit", "1"])
    assert [r.raw["stats"]["step"] for r in store.runs(phase=backlog_sweep.PHASE)] == ["asked", "queued"]


def _asked(st: S.Store, n: int, head: str, days_ago: int) -> None:
    at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    st.append_run({"phase": backlog_sweep.PHASE, "pr": n, "started": at, "finished": at,
                   "stats": {"step": "asked", "head_sha": head, "status": "executed"}})


def test_an_unanswered_ask_closes_after_two_weeks_once(store, repo, monkeypatch):
    from prospector_app.backend import handback
    closes: list[tuple[int, str, str | None]] = []
    monkeypatch.setattr(executor, "execute_pr", lambda n, action, *, dry_run:
                        closes.append((n, action.action, action.comment)) or {"status": "executed"})
    heads = {n: repo.pr(n, {f"f{n}.txt": "x\n"}) for n in (1, 2, 3)}
    for n, head in heads.items():
        _save(store, n, head, updated_at=RECENT)  # the ask itself updated the PR
    _asked(store, 1, heads[1], 15)
    _asked(store, 2, heads[2], 15)
    _asked(store, 3, heads[3], 5)
    asked_at = datetime.now(timezone.utc) - timedelta(days=15)
    monkeypatch.setattr(handback, "live_state", lambda n: {
        "state": "open", "head": heads[n], "author": f"dev{n}",
        "last_active": asked_at + timedelta(days=3) if n == 2 else asked_at - timedelta(days=90)})
    plans = _plans()
    assert (plans[1].bucket, plans[2].bucket, plans[3].bucket) == ("close", "close", "waiting")
    backlog_sweep.main(["--live", "--limit", "10"])
    assert [(n, a) for n, a, _ in closes] == [(1, "CLOSE_WONTFIX")]
    assert "reopen" in (closes[0][2] or "")
    data.refresh()
    backlog_sweep.main(["--live", "--limit", "10"])
    assert len(closes) == 1


def test_a_request_the_worker_failed_is_queued_once_more(store, repo):
    heads = {1: repo.pr(1, {"d.txt": "1\n"}), 2: repo.pr(2, {"e.txt": "2\n"})}
    repo.advance_base({"z.txt": "z\n"})
    for n, head in heads.items():
        _save(store, n, head)
    backlog_sweep.main(["--live", "--limit", "5"])

    def end(n: int, status: str, **result: object) -> None:
        rec = store.edit_pr(n)
        rec.record_fix_request(status, "rebase", head_sha=heads[n], result=result)
        data.refresh()

    end(1, "failed")
    end(2, "refused", detail="the PR diff is empty or unparsable")
    plans = _plans()
    assert plans[1].retry and plans[2].retry and plans[1].action == "rebase"
    backlog_sweep.main(["--live", "--limit", "5"])
    assert store.load_pr(1).fix_request["status"] == "queued"
    assert [r.raw["stats"]["step"] for r in store.runs(phase=backlog_sweep.PHASE)] == [
        "queued", "queued", "requeued", "requeued"]

    # A second machine ending is final.
    end(1, "failed")
    assert _plans()[1].bucket == "done"
