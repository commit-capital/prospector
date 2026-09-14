"""escalation: a trip files one issue per signature per week as the operator,
an offline worker is escalated once per silence, and neither raises."""
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import settings, worker_health
from pipeline import store as S
from prospector_app.backend import data, escalation


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    data.refresh()
    return st


class _Gh:
    def __init__(self, rc: int = 0):
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.rc = rc

    def __call__(self, argv, **kw):
        self.calls.append([str(a) for a in argv])
        self.envs.append(dict(kw.get("env") or {}))
        out = "https://github.com/o/meta/issues/42\n" if argv[1] == "issue" else ""
        return type("R", (), {"returncode": self.rc, "stdout": out, "stderr": "nope"})()


def test_file_issue_creates_the_label_then_the_issue_as_the_operator(monkeypatch):
    gh = _Gh()
    monkeypatch.setattr(escalation.subprocess, "run", gh)
    monkeypatch.setenv("GH_TOKEN", "bot-token")
    # Off a GitHub Actions runner, where the injected token is the sanctioned identity.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert escalation.file_issue("t", "b") == (42, "https://github.com/o/meta/issues/42")
    label, issue = gh.calls
    assert label[:3] == ["gh", "label", "create"] and escalation.LABEL in label
    assert issue[:3] == ["gh", "issue", "create"] and "--label" in issue
    assert "--repo" in issue and settings.feedback_repo() in issue
    assert "GH_TOKEN" not in gh.envs[-1]  # the bot token never reaches the meta-repo write


def test_file_issue_without_a_feedback_repo_files_nothing(monkeypatch):
    gh = _Gh()
    monkeypatch.setattr(escalation.subprocess, "run", gh)
    monkeypatch.setenv("PROSPECTOR_FEEDBACK_REPO", "")
    assert escalation.file_issue("t", "b") == (None, None)
    assert gh.calls == []


def test_file_issue_never_raises_on_a_broken_gh(monkeypatch):
    def boom(*a, **kw):
        raise OSError("gh missing")
    monkeypatch.setattr(escalation.subprocess, "run", boom)
    assert escalation.file_issue("t", "b") == (None, None)


def test_escalate_trip_ledgers_and_files_once_per_signature(store, monkeypatch):
    filed: list[str] = []
    monkeypatch.setattr(escalation, "file_issue",
                        lambda title, body: (filed.append(title) or 5, "u/5"))
    me = settings.worker_id()
    worker_health.update(store, me, lambda r: worker_health.trip(
        r, "fix", kind="sandbox", reason="docker down at /x"))
    escalation.escalate_trip(["fix"])
    # A second trip with the same shape within the week files nothing new.
    worker_health.update(store, me, lambda r: worker_health.reopen(r, "fix", by="t"))
    worker_health.update(store, me, lambda r: worker_health.trip(
        r, "fix", kind="sandbox", reason="docker down at /y"))
    escalation.escalate_trip(["fix"])
    assert len(filed) == 1 and "fix lane tripped" in filed[0]
    trips = [r for r in store.runs() if getattr(r, "phase", "") == "worker:trip"]
    assert len(trips) == 2
    issue = worker_health.load(store, me)["lanes"]["fix"]["issue"]
    assert issue["number"] == 5 and issue["url"] == "u/5"


def test_offline_workers_are_escalated_once_per_silence(store, monkeypatch):
    filed: list[str] = []
    monkeypatch.setattr(escalation, "file_issue",
                        lambda title, body: (filed.append(title) or 9, "u/9"))
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    dark = (now - timedelta(hours=5)).isoformat()
    fresh = (now - timedelta(seconds=10)).isoformat()
    store.save_verify_worker({"host": "studio", "last_beat": dark, "autohunt": True})
    store.save_fix_worker({"host": "studio", "last_beat": dark, "autohunt": True})
    store.save_verify_worker({"host": "laptop", "last_beat": fresh, "autohunt": True})
    assert escalation.check_offline_workers(now) == ["studio"]
    assert escalation.check_offline_workers(now + timedelta(minutes=30)) == []
    assert filed == ["[worker-health] studio: worker offline"]
    entries = [r for r in store.runs() if getattr(r, "phase", "") == "worker:offline"]
    assert len(entries) == 1 and entries[0].raw["stats"]["host"] == "studio"


def test_resume_reopens_and_ledgers(store):
    me = settings.worker_id()
    worker_health.update(store, me, lambda r: worker_health.trip(
        r, "verify", kind="pin-refresh", reason="x"))
    escalation.resume(me, "verify")
    assert not worker_health.is_tripped(worker_health.load(store, me), "verify")
    with pytest.raises(ValueError):
        escalation.resume(me, "teleport")


def test_health_status_lists_tripped_workers_first(store):
    worker_health.update(store, "b", lambda r: worker_health.record_success(r, "fix"))
    worker_health.update(store, "a", lambda r: worker_health.trip(r, "fix", kind="k", reason="r"))
    worker_health.update(store, "c", lambda r: worker_health.trip(r, "verify", kind="k", reason="r"))
    st = escalation.health_status()
    assert [h["host"] for h in st["hosts"]] == ["a", "c", "b"]
    assert st["any_tripped"] is True
    assert st["hosts"][0]["tripped"] == ["fix"]
