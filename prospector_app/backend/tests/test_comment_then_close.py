"""The shared comment-then-close write path. Two invariants the four callers used to
implement separately, and drifted on: every exit lands in the activity log, and the
idempotency check is scoped to this action's own comment. GitHub is never touched."""
from prospector_app.backend import data
from prospector_app.backend import executor
from prospector_app.backend import models


class _Res:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode, self.stderr, self.stdout = returncode, stderr, stdout


def _capture_activity(monkeypatch) -> list[tuple]:
    recorded = []
    monkeypatch.setattr(executor.activity, "record",
                        lambda verb, **k: recorded.append((verb, k)))
    return recorded


def _call(**kw) -> dict:
    """_comment_then_close with the argv/base plumbing filled in."""
    return executor._comment_then_close(
        7, base={"pr": 7, "action": "CLOSE"}, idempotency_key="marker",
        comment_argv=["gh", "pr", "comment", "7"], close_argv=["gh", "pr", "close", "7"],
        token="tok", log_verb="execute", **kw)


# --- every exit is logged (the trust model requires every write attempt recorded) ---

def test_comment_failure_is_recorded(monkeypatch):
    monkeypatch.setattr(executor, "_has_bot_comment", lambda n, contains=None: False)
    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: _Res(1, "rate limited"))
    recorded = _capture_activity(monkeypatch)
    res = _call()
    assert res["status"] == "error" and "comment failed" in res["detail"]
    assert recorded and recorded[0][1]["status"] == "error"


def test_close_failure_is_recorded(monkeypatch):
    monkeypatch.setattr(executor, "_has_bot_comment", lambda n, contains=None: False)
    monkeypatch.setattr(executor, "bot_run",
                        lambda argv, token, **kw: _Res(0) if "comment" in argv else _Res(1, "boom"))
    recorded = _capture_activity(monkeypatch)
    res = _call()
    assert res["status"] == "error" and "close failed" in res["detail"]
    assert recorded and recorded[0][1]["status"] == "error"


def test_exception_is_recorded(monkeypatch):
    monkeypatch.setattr(executor, "_has_bot_comment", lambda n, contains=None: False)

    def _boom(argv, token, **kw):
        raise RuntimeError("token revoked")

    monkeypatch.setattr(executor, "bot_run", _boom)
    recorded = _capture_activity(monkeypatch)
    res = _call()
    assert res["status"] == "error" and "token revoked" in res["detail"]
    assert recorded and recorded[0][1]["status"] == "error"


def test_success_records_and_runs_on_success(monkeypatch):
    monkeypatch.setattr(executor, "_has_bot_comment", lambda n, contains=None: False)
    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: _Res(0))
    recorded = _capture_activity(monkeypatch)
    ran = []
    res = _call(on_success=lambda: ran.append(True))
    assert res["status"] == "executed" and res["detail"] == "commented + closed"
    assert ran == [True] and recorded[0][1]["status"] == "executed"


def test_existing_matching_comment_is_not_reposted(monkeypatch):
    monkeypatch.setattr(executor, "_has_bot_comment", lambda n, contains=None: True)
    calls = []
    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: (calls.append(argv), _Res(0))[1])
    _capture_activity(monkeypatch)
    res = _call()
    assert res["detail"] == "comment-exists + closed"
    assert not any("comment" in c for c in calls)  # only the close ran


def test_event_url_is_opt_in(monkeypatch):
    """Issue closes don't carry event_url; the PR close does."""
    monkeypatch.setattr(executor, "_has_bot_comment", lambda n, contains=None: False)
    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: _Res(0, stdout="https://x/#c1"))
    _capture_activity(monkeypatch)
    assert "event_url" not in _call()
    assert _call(capture_event_url=True)["event_url"] == "https://x/#c1"


# --- the idempotency key is scoped to this action's own comment ---

def test_comment_marker_is_a_verbatim_substring_of_the_first_line():
    """The marker must survive round-tripping: it is matched against the posted body,
    so mangling quotes or backslashes out of it would make it never match."""
    body = 'Closing as a duplicate of #99 — "dupe" (see below) \\ x\nsecond line'
    marker = executor._comment_marker(body)
    assert marker is not None and marker in body
    assert "\n" not in marker and len(marker) <= 40
    assert executor._comment_marker("   ") is None
    assert executor._comment_marker("") is None


def test_execute_pr_posts_its_comment_despite_an_unrelated_bot_comment(monkeypatch):
    """A prior bot comment on the PR — e.g. the "@greptileai" re-trigger — must not
    suppress the closing comment, or the PR gets closed with no explanation."""
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {123: [5]})
    monkeypatch.setattr(data, "prs", lambda: {123: {"meta": {}}})
    monkeypatch.setattr(executor, "_pr_live", lambda n: None)  # preflight fail-open
    monkeypatch.setattr(executor, "_reflect_state", lambda *a, **k: None)
    _capture_activity(monkeypatch)
    # the only bot comment on the thread is the Greptile re-trigger
    monkeypatch.setattr(executor, "_has_bot_comment",
                        lambda n, contains=None: contains is None or contains in "@greptileai")
    calls = []
    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: (calls.append(argv), _Res(0))[1])

    res = executor.execute_pr(123, models.CloseAction(action="CLOSE"), token="tok", dry_run=False)
    assert res["status"] == "executed"
    assert res["detail"] == "commented + closed", "the closing comment must still be posted"
    assert any("comment" in c for c in calls)
