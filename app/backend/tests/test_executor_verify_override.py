"""merge_pr with an operator override on a verify `escalate` outcome: the block
opens when a reason is supplied, and the reason is logged to the store as the
verify override (Pr.log_verify_override) BEFORE the merge — never on dry-run,
never to the security section (the two overrides are disambiguated)."""
from types import SimpleNamespace

from app.backend import caps
from app.backend import data
from app.backend import executor
from pipeline import gates


class _EditPr:
    def __init__(self, events: list):
        self._events = events

    def log_security_override(self, reason: str, *, by: str | None = None) -> None:
        self._events.append(("security-override", reason, by))

    def log_verify_override(self, reason: str, *, by: str | None = None) -> None:
        self._events.append(("verify-override", reason, by))


class _Store:
    def __init__(self, events: list):
        self._events = events

    def edit_pr(self, n: int) -> _EditPr:
        return _EditPr(self._events)


def _setup(monkeypatch, events: list, *, escalate=True, overridable=True):
    rec = SimpleNamespace(head_sha="h", linked_issues=[])
    monkeypatch.setattr(data, "prs", lambda: {5: rec})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    monkeypatch.setattr(data, "store", lambda: _Store(events))
    monkeypatch.setattr(data, "refresh", lambda: None)
    monkeypatch.setattr(executor, "_changed_paths", lambda n: [])
    # the verify escalate blocks unless a reason is supplied
    monkeypatch.setattr(
        gates, "merge_eligibility",
        lambda rec, today=None, changed_paths=None, override_reason=None:
            (True, "passed") if not escalate or override_reason
            else (False, "dynamic verification escalate"))
    # only the verify override is pending (a GREEN-security, escalate-verify PR)
    monkeypatch.setattr(gates, "security_overridable", lambda rec, **k: False)
    monkeypatch.setattr(gates, "verify_overridable", lambda rec, **k: overridable and escalate)
    monkeypatch.setattr(executor, "_pr_live",
                        lambda n: {"state": "open", "merged": False, "head": "h",
                                   "mergeable_state": "clean"})
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "tok")
    monkeypatch.setattr(caps, "capabilities", lambda: {"login": "operator"})
    monkeypatch.setattr(executor, "_reflect_state", lambda n, **k: None)
    monkeypatch.setattr(executor, "_credit_merge_closed_issues", lambda *a, **k: None)

    def fake_merge(cmd, token):
        events.append(("merge", cmd))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(executor, "bot_merge_run", fake_merge)


def test_escalate_without_reason_stays_blocked(monkeypatch):
    events: list = []
    _setup(monkeypatch, events)
    res = executor.merge_pr(5, dry_run=True)
    assert res["status"] == "blocked" and "escalate" in res["detail"]
    assert events == []


def test_dry_run_previews_the_verify_override_without_writing(monkeypatch):
    events: list = []
    _setup(monkeypatch, events)
    res = executor.merge_pr(5, dry_run=True, reason="read the red output; the test is fine")
    assert res["status"] == "dry-run"
    assert "verify-escalate override" in res["detail"]
    assert res["verify_override"] == "read the red output; the test is fine"
    assert events == []   # dry-run never touches the store


def test_live_merge_logs_the_verify_override_before_merging(monkeypatch):
    events: list = []
    _setup(monkeypatch, events)
    res = executor.merge_pr(5, dry_run=False, reason="reproduced by hand; genuine fix")
    assert res["status"] == "merged"
    # the verify override is logged, and to the VERIFY section, before the merge
    assert [e[0] for e in events] == ["verify-override", "merge"]
    assert events[0] == ("verify-override", "reproduced by hand; genuine fix", "operator")
