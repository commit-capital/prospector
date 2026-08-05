"""Operator-directed issue close: the four-disposition gate (not-planned /
completed / fixed / dup) and the executor path that posts a comment and closes
with the disposition's state_reason. fixed/dup are operator-asserted (light live
checks) and attribute to CLOSE_ISSUE_FIXED / CLOSE_ISSUE_DUP. The store is seeded
in a temp dir; GitHub is never touched."""
from prospector_app.backend import executor
from prospector_app.backend import issues
from prospector_app.backend import models
from issue_triage import issue_store


def _seed(tmp_path, monkeypatch):
    """One open issue (#10) in a temp store, with the live-state check stubbed open."""
    monkeypatch.setattr(issues, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(issues, "_live_state", lambda n: "open")
    monkeypatch.setattr(issues, "_live_pr_states", lambda nums: {n: "merged" for n in nums})
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(10, {"title": "crash on boot", "state": "open", "author": "al",
                         "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z"})
    return st


# --- gate: plain closes ---

def test_close_gate_ok_for_open_issue(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    ok, why = issues.close_gate(10, "not-planned", "closing as stale", None, None)
    assert ok, why


def test_close_gate_requires_comment(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    ok, why = issues.close_gate(10, "completed", "   ", None, None)
    assert not ok and "comment" in why


def test_close_gate_rejects_bad_disposition(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    ok, why = issues.close_gate(10, "wontfix", "x", None, None)
    assert not ok and "disposition" in why


def test_close_gate_unknown_issue(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    ok, why = issues.close_gate(999, "not-planned", "x", None, None)
    assert not ok and "not in store" in why


def test_close_gate_blocks_already_closed(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(issues, "_live_state", lambda n: "closed")
    ok, why = issues.close_gate(10, "not-planned", "x", None, None)
    assert not ok and "already closed" in why


# --- gate: fixed (operator-asserted, live merged check) ---

def test_close_gate_fixed_requires_fixer(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    ok, why = issues.close_gate(10, "fixed", None, None, None)
    assert not ok and "fixer PR" in why


def test_close_gate_fixed_blocks_unmerged(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(issues, "_live_pr_states", lambda nums: {123: "open"})
    ok, why = issues.close_gate(10, "fixed", None, 123, None)
    assert not ok and "not merged" in why


def test_close_gate_fixed_ok_when_merged(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)  # _live_pr_states stub returns merged; comment optional
    ok, why = issues.close_gate(10, "fixed", None, 123, None)
    assert ok, why


# --- gate: dup (operator-asserted canonical) ---

def test_close_gate_dup_requires_canonical(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    ok, why = issues.close_gate(10, "dup", None, None, None)
    assert not ok and "canonical" in why


def test_close_gate_dup_allows_closed_not_planned_canonical(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    calls = []

    def live_state(n):
        calls.append(n)
        return "closed" if n == 20 else "open"

    monkeypatch.setattr(issues, "_live_state", live_state)
    ok, why = issues.close_gate(10, "dup", None, None, 20)
    assert ok, why
    assert calls == [10]


def test_close_gate_dup_allows_fixed_canonical(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(issues, "_live_state", lambda n: "closed" if n == 20 else "open")
    ok, why = issues.close_gate(10, "dup", None, None, 20)
    assert ok, why


def test_close_gate_dup_ok(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)  # every issue stubbed open; comment optional
    ok, why = issues.close_gate(10, "dup", None, None, 20)
    assert ok, why


# --- executor: plain ---

def test_close_issue_with_comment_dry_run(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    res = executor.close_issue_with_comment(
        10, models.IssueCloseBody(disposition="not-planned", comment="stale, closing"),
        token=None, dry_run=True)
    assert res["status"] == "dry-run"
    assert res["action"] == "CLOSE_ISSUE"
    assert "close issue #10" in res["detail"] and "not planned" in res["detail"]


def test_close_issue_with_comment_blocked_empty(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    res = executor.close_issue_with_comment(
        10, models.IssueCloseBody(disposition="completed", comment="  "),
        token=None, dry_run=True)
    assert res["status"] == "blocked" and "comment" in res["detail"]


def test_close_issue_with_comment_live_posts_and_closes(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(executor, "_has_bot_comment", lambda n, contains=None: False)
    calls = []

    class _Ok:
        returncode = 0
        stderr = ""

    def _fake_bot_run(argv, token, **kw):
        calls.append(argv)
        return _Ok()

    monkeypatch.setattr(executor, "bot_run", _fake_bot_run)
    res = executor.close_issue_with_comment(
        10, models.IssueCloseBody(disposition="completed", comment="closing as done"),
        token="tok", dry_run=False)
    assert res["status"] == "executed"
    assert ["gh", "issue", "comment", "10", "--repo", executor.REPO, "--body", "closing as done"] in calls
    assert ["gh", "issue", "close", "10", "--repo", executor.REPO, "--reason", "completed"] in calls


# --- executor: fixed / dup attribution + templated comment ---

def test_close_issue_fixed_attributes_and_templates(tmp_path, monkeypatch):
    """An empty-comment 'fixed' close lands as CLOSE_ISSUE_FIXED, closes 'completed',
    and falls back to the templated 'fixed by #N' comment."""
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    res = executor.close_issue_with_comment(
        10, models.IssueCloseBody(disposition="fixed", fixed_by=123),
        token=None, dry_run=True)
    assert res["status"] == "dry-run"
    assert res["action"] == "CLOSE_ISSUE_FIXED" and res["reason"] == "completed"
    assert res["fixed_by"] == 123 and "#123" in res["detail"]


def test_close_issue_dup_attributes_and_templates(tmp_path, monkeypatch):
    """An empty-comment 'dup' close lands as CLOSE_ISSUE_DUP, closes 'duplicate',
    and falls back to the templated 'duplicate of #N' comment."""
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    res = executor.close_issue_with_comment(
        10, models.IssueCloseBody(disposition="dup", canonical=20),
        token=None, dry_run=True)
    assert res["status"] == "dry-run"
    assert res["action"] == "CLOSE_ISSUE_DUP" and res["reason"] == "duplicate"
    assert res["canonical"] == 20 and "#20" in res["detail"]


def _capture_bot_run(monkeypatch):
    """Stub bot_run to capture argv and _has_bot_comment to force a fresh comment."""
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(executor, "_has_bot_comment", lambda n, contains=None: False)
    calls = []

    class _Ok:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: (calls.append(argv), _Ok())[1])
    return calls


def test_close_issue_fixed_live_posts_template_and_closes_completed(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)  # _live_pr_states stub reports #123 merged
    calls = _capture_bot_run(monkeypatch)
    res = executor.close_issue_with_comment(
        10, models.IssueCloseBody(disposition="fixed", fixed_by=123),
        token="tok", dry_run=False)
    assert res["status"] == "executed"
    comment_argv = next(c for c in calls if c[2] == "comment")
    assert "#123" in comment_argv[-1]  # templated "fixed by #123" body
    assert ["gh", "issue", "close", "10", "--repo", executor.REPO, "--reason", "completed"] in calls


def test_close_issue_dup_live_posts_template_and_closes_duplicate_of(tmp_path, monkeypatch):
    """A dup close carries --duplicate-of, so GitHub records the marked_as_duplicate
    link on the canonical issue rather than a bare state_reason."""
    _seed(tmp_path, monkeypatch)
    calls = _capture_bot_run(monkeypatch)
    res = executor.close_issue_with_comment(
        10, models.IssueCloseBody(disposition="dup", canonical=20),
        token="tok", dry_run=False)
    assert res["status"] == "executed"
    comment_argv = next(c for c in calls if c[2] == "comment")
    assert "#20" in comment_argv[-1]  # templated "duplicate of #20" body
    assert ["gh", "issue", "close", "10", "--repo", executor.REPO,
            "--reason", "duplicate", "--duplicate-of", "20"] in calls


def test_issue_close_argv_keys_duplicate_of_on_the_reason():
    """Both dup-close paths share this argv builder: --duplicate-of rides on a
    'duplicate' reason with a known canonical, and on nothing else."""
    assert executor._issue_close_argv(10, "duplicate", 20)[-2:] == ["--duplicate-of", "20"]
    assert "--duplicate-of" not in executor._issue_close_argv(10, "duplicate", None)
    assert "--duplicate-of" not in executor._issue_close_argv(10, "completed", 20)
    assert "--duplicate-of" not in executor._issue_close_argv(10, "not planned", 20)


def test_close_issue_fixed_carries_no_duplicate_of(tmp_path, monkeypatch):
    """--duplicate-of is scoped to the dup disposition; a 'fixed' close never carries it."""
    _seed(tmp_path, monkeypatch)
    calls = _capture_bot_run(monkeypatch)
    executor.close_issue_with_comment(
        10, models.IssueCloseBody(disposition="fixed", fixed_by=123),
        token="tok", dry_run=False)
    close_argv = next(c for c in calls if c[2] == "close")
    assert "--duplicate-of" not in close_argv


def test_close_issue_with_comment_logs_on_write_failure(tmp_path, monkeypatch):
    """A live write that exits non-zero (rate limit, transient error) still lands in
    the activity log — the trust model requires every upstream write attempt logged."""
    _seed(tmp_path, monkeypatch)
    recorded = []
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: recorded.append((a, k)))
    monkeypatch.setattr(executor, "_has_bot_comment", lambda n, contains=None: False)

    class _Fail:
        returncode = 1
        stderr = "rate limited"

    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: _Fail())
    res = executor.close_issue_with_comment(
        10, models.IssueCloseBody(disposition="not-planned", comment="closing"),
        token="tok", dry_run=False)
    assert res["status"] == "error" and "comment failed" in res["detail"]
    assert recorded, "a failed upstream write must still be logged"
