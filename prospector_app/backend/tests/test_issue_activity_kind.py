"""Issue actions carry the semantic ``issue-close`` kind through the activity
log, so the dashboard's OUR ACTIONS · ISSUES cards count them — and the PR
cards never do. Covers the normalize path (write + legacy read), the
``issue_action_counts`` fold, the ``summarize`` split, and the executor's
recorded kind."""
from prospector_app.backend import activity
from prospector_app.backend import executor
from prospector_app.backend import models
from issue_triage.issue_model import Issue


def _ev(**kw):
    base = {"at": "2026-07-06T12:00:00+00:00", "status": "executed", "dry_run": False}
    base.update(kw)
    return base


def _issue(n: int, state: str) -> Issue:
    return Issue(None, {"issue": n, "meta": {"state": state}})


# ── canonical_kind / normalize ────────────────────────────────────────────────

def test_close_issue_action_maps_to_issue_close():
    ev = {"kind": "execute-issue", "action": "CLOSE_ISSUE_DUP", "issue": 5}
    assert activity.canonical_kind(ev) == "issue-close"
    assert activity.normalize(ev)["kind"] == "issue-close"


def test_legacy_row_stored_under_pr_kind_heals_on_read():
    """A row written before issue kinds existed carries kind='comment' with a
    CLOSE_ISSUE_* action; normalize reads it as issue-close."""
    ev = {"kind": "comment", "action": "CLOSE_ISSUE_FIXED", "issue": 9}
    assert activity.canonical_kind(ev) == "issue-close"


def test_semantic_issue_close_kind_is_stable():
    assert activity.canonical_kind({"kind": "issue-close", "action": "CLOSE_ISSUE_DUP"}) == "issue-close"


def test_pr_close_and_comment_kinds_unaffected():
    assert activity.canonical_kind({"kind": "execute", "action": "CLOSE_DUP"}) == "close"
    assert activity.canonical_kind({"kind": "comment", "action": None}) == "comment"


# ── issue_action_counts ───────────────────────────────────────────────────────

def test_issue_action_counts_landed_only():
    events = [
        activity.normalize(_ev(kind="issue-close", action="CLOSE_ISSUE_DUP", issue=1)),
        activity.normalize(_ev(kind="issue-close", action="CLOSE_ISSUE_DUP", issue=2)),
        activity.normalize(_ev(kind="issue-close", action="CLOSE_ISSUE_FIXED", issue=3, dry_run=True, status="dry-run")),
        activity.normalize(_ev(kind="issue-close", action="CLOSE_ISSUE_DUP", issue=4, status="blocked")),
        activity.normalize(_ev(kind="close", action="CLOSE_DUP", pr=5)),
    ]
    assert activity.issue_action_counts(events) == {"CLOSE_ISSUE_DUP": 2}


# ──── issue_progress ────────────────────────────────────────────────────────────────────────

def test_issue_progress_counts_landed_closes_against_open_total():
    issues = {1: _issue(1, "open"), 2: _issue(2, "open"), 3: _issue(3, "closed")}
    events = [
        _ev(at="2026-07-06T12:00:00+00:00", kind="issue-close", action="CLOSE_ISSUE_FIXED", issue=3),
        _ev(at="2026-07-06T11:00:00+00:00", kind="issue-close", action="CLOSE_ISSUE_DUP", issue=1),
    ]
    progress = activity.issue_progress(issues, events)
    assert progress == {
        "open_total": 2,
        "universe": 3,
        "actioned": 2,
        "remaining": 1,
        "closed": 2,
        "by_reason": {"already-fixed": 1, "duplicate": 1},
        "pct": 66.7,
    }


def test_issue_progress_reopen_returns_issue_to_backlog():
    issues = {1: _issue(1, "open")}
    events = [
        _ev(at="2026-07-06T12:00:00+00:00", kind="issue-reopen", action="REOPEN_ISSUE", issue=1,
            status="reopened"),
        _ev(at="2026-07-06T11:00:00+00:00", kind="issue-close", action="CLOSE_ISSUE_DUP", issue=1),
    ]
    progress = activity.issue_progress(issues, events)
    assert progress["actioned"] == 0
    assert progress["remaining"] == 1
    assert progress["pct"] == 0.0


def test_issue_progress_ignores_dry_runs_and_non_state_actions():
    issues = {1: _issue(1, "open"), 2: _issue(2, "open")}
    events = [
        _ev(kind="comment", action="COMMENT_ISSUE", issue=1),
        _ev(kind="issue-close", action="CLOSE_ISSUE_DUP", issue=1),
        _ev(kind="issue-close", action="CLOSE_ISSUE_FIXED", issue=2, dry_run=True, status="dry-run"),
    ]
    progress = activity.issue_progress(issues, events)
    assert progress["actioned"] == 1
    assert progress["remaining"] == 1


# ── summarize keeps PR and issue tallies disjoint ─────────────────────────────

def test_summarize_counts_issue_close_separately():
    events = [
        activity.normalize(_ev(kind="issue-close", action="CLOSE_ISSUE_DUP", issue=1)),
        activity.normalize(_ev(kind="close", action="CLOSE_DUP", pr=2)),
        activity.normalize(_ev(kind="comment", pr=3)),
    ]
    s = activity.summarize(events, group_by="day")
    assert s["totals"]["issue-close"] == 1
    assert s["totals"]["close"] == 1
    assert s["totals"]["comment"] == 1
    assert all(b["issue-close"] == 1 for b in s["buckets"])


# ── executor records the semantic kind ────────────────────────────────────────

def test_close_issue_records_issue_close_kind(monkeypatch):
    from prospector_app.backend import issues as issues_mod
    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(executor.activity, "record",
                        lambda kind, **kw: recorded.append((kind, kw)) or {})
    monkeypatch.setattr(issues_mod, "close_dup_gate", lambda n: (True, "ok"))
    res = executor.close_issue(7, models.IssueCloseDupBody(canonical=3, comment="dup of #3"),
                               token=None, dry_run=True)
    assert res["status"] == "dry-run"
    assert recorded and recorded[0][0] == "issue-close"
    assert recorded[0][1]["action"] == "CLOSE_ISSUE_DUP"


def test_issue_reopen_is_a_disjoint_kind():
    """An issue reopen carries its own semantic kind, so it never lands in the PR
    ``reopen`` tally that drives PR-reopened velocity."""
    assert activity.canonical_kind({"kind": "issue-reopen", "action": "REOPEN_ISSUE", "issue": 5}) == "issue-reopen"
    assert activity.canonical_kind({"kind": "reopen", "action": "REOPEN", "pr": 5}) == "reopen"


def test_reopen_issue_records_issue_reopen_kind(monkeypatch):
    from prospector_app.backend import issues as issues_mod
    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(executor.activity, "record",
                        lambda kind, **kw: recorded.append((kind, kw)) or {})
    monkeypatch.setattr(executor, "_bot_comment_ids", lambda n: [])
    monkeypatch.setattr(issues_mod, "reflect_issue_state", lambda n, state: None)

    class _Ok:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: _Ok())
    res = executor.reopen_issue(6, token="tok", dry_run=False)
    assert res["status"] == "reopened"
    assert recorded and recorded[0][0] == "issue-reopen"


def test_close_issue_fixed_records_issue_close_kind(monkeypatch):
    from prospector_app.backend import issues as issues_mod
    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(executor.activity, "record",
                        lambda kind, **kw: recorded.append((kind, kw)) or {})
    monkeypatch.setattr(issues_mod, "close_fixed_gate", lambda n, fixed_by: (True, "ok"))
    res = executor.close_issue_fixed(8, models.IssueCloseFixedBody(fixed_by=42, comment="fixed by #42"),
                                     token=None, dry_run=True)
    assert res["status"] == "dry-run"
    assert recorded and recorded[0][0] == "issue-close"
    assert recorded[0][1]["action"] == "CLOSE_ISSUE_FIXED"
