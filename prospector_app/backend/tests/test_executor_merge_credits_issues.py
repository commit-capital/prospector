"""A merge that lands a fix closes the PR's explicitly-linked issues upstream (via
the Fixes/Closes keyword), so the executor credits each such close into the issue
bucket of the activity log — the strongest form of issue action. Even though GitHub,
not our executor, runs the close, the ledger records it as CLOSE_ISSUE_FIXED /
via=merge so 'Our actions · Issues' counts it."""
from types import SimpleNamespace

from prospector_app.backend import executor
from prospector_app.backend import issues as issues_mod
from pipeline import settings


def _capture(monkeypatch, *, states=None):
    """Record activity.record calls and reflect_issue_state calls; stub the issue
    store's known state from `states` (issue -> 'open'/'closed'; absent -> None)."""
    recorded, reflected = [], []
    states = states or {}
    monkeypatch.setattr(executor.activity, "record",
                        lambda kind, **f: recorded.append({"kind": kind, **f}))
    monkeypatch.setattr(issues_mod, "issue_state", lambda n: states.get(int(n)))
    monkeypatch.setattr(issues_mod, "reflect_issue_state",
                        lambda n, state, reason=None:
                            reflected.append((int(n), state, reason)))
    return recorded, reflected


def test_merge_credits_each_explicit_open_issue(monkeypatch):
    rec = SimpleNamespace(linked_issues=[
        {"issue": 8723, "how": "explicit"},
        {"issue": 900, "how": "subsystem"},   # not a closing link — skipped
        {"issue": 901, "how": "issue-ref"},   # not a closing link — skipped
    ])
    recorded, reflected = _capture(monkeypatch, states={8723: "open"})

    executor._credit_merge_closed_issues(rec, 8722, dry_run=False)

    assert recorded == [{
        "kind": "issue-close", "identity": settings.bot_login(), "dry_run": False,
        "issue": 8723, "action": "CLOSE_ISSUE_FIXED", "fixed_by": 8722, "via": "merge",
        "status": "closed", "detail": "closed by merge of #8722",
    }]
    assert reflected == [(8723, "closed", "completed")]


def test_merge_skips_already_closed_issue(monkeypatch):
    rec = SimpleNamespace(linked_issues=[{"issue": 55, "how": "explicit"}])
    recorded, reflected = _capture(monkeypatch, states={55: "closed"})

    executor._credit_merge_closed_issues(rec, 10, dry_run=False)

    assert recorded == []       # our merge didn't close it — no credit
    assert reflected == []


def test_merge_credits_issue_absent_from_store(monkeypatch):
    """An explicit link whose issue we've never ingested (state None) is still an
    auto-close on merge — credit it rather than lose the strongest action."""
    rec = SimpleNamespace(linked_issues=[{"issue": 77, "how": "explicit"}])
    recorded, _ = _capture(monkeypatch, states={})

    executor._credit_merge_closed_issues(rec, 11, dry_run=False)

    assert [e["issue"] for e in recorded] == [77]


def test_merge_dedups_repeated_issue(monkeypatch):
    rec = SimpleNamespace(linked_issues=[
        {"issue": 42, "how": "explicit"}, {"issue": 42, "how": "explicit"}])
    recorded, reflected = _capture(monkeypatch, states={42: "open"})

    executor._credit_merge_closed_issues(rec, 12, dry_run=False)

    assert [e["issue"] for e in recorded] == [42]
    assert reflected == [(42, "closed", "completed")]


def test_dry_run_previews_without_mutating(monkeypatch):
    rec = SimpleNamespace(linked_issues=[{"issue": 8723, "how": "explicit"}])
    recorded, reflected = _capture(monkeypatch, states={8723: "open"})

    executor._credit_merge_closed_issues(rec, 8722, dry_run=True)

    assert recorded[0]["status"] == "dry-run"
    assert recorded[0]["dry_run"] is True
    assert reflected == []       # dry-run mutates nothing upstream or in the store


def test_no_rec_is_noop(monkeypatch):
    recorded, reflected = _capture(monkeypatch)
    executor._credit_merge_closed_issues(None, 1, dry_run=False)
    assert recorded == [] and reflected == []
