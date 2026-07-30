"""merge_pr with an operator override reason: a YELLOW-blocked gate opens when a
reason is supplied, and the reason is logged to the store as the verdict's
override BEFORE the merge executes — never on dry-run, never when the gate
passes on its own."""
from types import SimpleNamespace

from prospector_app.backend import caps
from prospector_app.backend import data
from prospector_app.backend import executor
from pipeline import gates


class _EditPr:
    def __init__(self, events: list):
        self._events = events

    def log_security_override(self, reason: str, *, by: str | None = None) -> None:
        self._events.append(("override", reason, by))


class _Store:
    def __init__(self, events: list):
        self._events = events

    def edit_pr(self, n: int) -> _EditPr:
        return _EditPr(self._events)


def _setup(monkeypatch, events: list, *, verdict="YELLOW", overridable=True):
    rec = SimpleNamespace(head_sha="h", security_verdict=verdict, linked_issues=[])
    monkeypatch.setattr(data, "prs", lambda: {5: rec})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    monkeypatch.setattr(data, "store", lambda: _Store(events))
    monkeypatch.setattr(data, "refresh", lambda: None)
    monkeypatch.setattr(executor, "_changed_paths", lambda n: [])
    # gate: YELLOW blocks unless a reason is supplied; GREEN always passes
    monkeypatch.setattr(
        gates, "merge_eligibility",
        lambda rec, today=None, changed_paths=None, override_reason=None:
            (True, "passed") if verdict == "GREEN" or override_reason
            else (False, f"security {verdict}"))
    monkeypatch.setattr(gates, "security_overridable",
                        lambda rec, today=None, changed_paths=None: overridable)
    # this PR's block is security, not verify — no verify override is pending
    monkeypatch.setattr(gates, "verify_overridable",
                        lambda rec, today=None, changed_paths=None: False)
    monkeypatch.setattr(executor, "_pr_live",
                        lambda n: {"state": "open", "merged": False, "head": "h",
                                   "mergeable_state": "clean"})
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "tok")
    monkeypatch.setattr(caps, "capabilities", lambda: {"login": "operator"})

    def fake_merge(cmd, token):
        events.append(("merge", cmd))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(executor, "bot_merge_run", fake_merge)
    monkeypatch.setattr(executor, "_reflect_state", lambda n, **k: None)


def test_yellow_without_reason_stays_blocked(monkeypatch):
    events: list = []
    _setup(monkeypatch, events)
    res = executor.merge_pr(5, dry_run=True)
    assert res["status"] == "blocked"
    assert "security YELLOW" in res["detail"]
    assert events == []


def test_dry_run_previews_override_without_writing(monkeypatch):
    events: list = []
    _setup(monkeypatch, events)
    res = executor.merge_pr(5, dry_run=True, reason="mirrors master's stdout ignore list")
    assert res["status"] == "dry-run"
    assert "override" in res["detail"]
    assert res["security_override"] == "mirrors master's stdout ignore list"
    assert events == []  # dry-run never touches the store


def test_live_merge_logs_override_before_merging(monkeypatch):
    events: list = []
    _setup(monkeypatch, events)
    res = executor.merge_pr(5, dry_run=False, reason="OOM fix outweighs log-field loss")
    assert res["status"] == "merged"
    assert res["security_override"] == "OOM fix outweighs log-field loss"
    assert [e[0] for e in events] == ["override", "merge"]
    assert events[0] == ("override", "OOM fix outweighs log-field loss", "operator")


def test_reason_on_a_passing_gate_writes_nothing(monkeypatch):
    events: list = []
    _setup(monkeypatch, events, verdict="GREEN", overridable=False)
    res = executor.merge_pr(5, dry_run=False, reason="unneeded note")
    assert res["status"] == "merged"
    assert "security_override" not in res
    assert [e[0] for e in events] == ["merge"]  # no override write
