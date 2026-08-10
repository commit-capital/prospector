"""The safety guard is the load-bearing read-only enforcement. Test it hard."""
import pytest

from prospector_app.backend import safety_guard as sg

BLOCKED = [
    ["gh", "pr", "comment", "5", "--body", "x"],
    ["gh", "pr", "close", "5"],
    ["gh", "pr", "edit", "5", "--title", "x"],
    ["gh", "pr", "merge", "5"],
    ["gh", "pr", "review", "5", "--approve"],
    ["gh", "pr", "create", "--title", "x"],
    ["gh", "pr", "reopen", "5"],
    ["gh", "pr", "ready", "5"],
    ["gh", "issue", "create", "--title", "x"],
    ["gh", "issue", "close", "5"],
    ["gh", "issue", "comment", "5", "--body", "x"],
    ["gh", "api", "-X", "POST", "repos/x/issues/5/comments"],
    ["gh", "api", "--method", "POST", "repos/x"],
    ["gh", "api", "-X", "PATCH", "repos/x"],
    ["gh", "api", "-X", "DELETE", "repos/x"],
    ["gh", "api", "-X", "PUT", "repos/x"],
    ["git", "push", "origin", "main"],
    ["curl", "-X", "POST", "https://api.github.com/repos/x"],
    ["curl", "--request", "DELETE", "https://api.github.com/x"],
    ["rm", "-rf", "/"],            # not an allowed binary
    ["bash", "-c", "gh pr close 5"],  # not an allowed binary
]

ALLOWED = [
    ["gh", "pr", "diff", "5", "--repo", "test-owner/test-repo"],
    ["gh", "api", "repos/test-owner/test-repo/pulls/5"],
    ["gh", "api", "repos/x/commits/abc/check-runs"],
    ["git", "log", "--oneline"],
    ["git", "status"],
    ["claude", "-p", "/diagnose-pr-cluster 29"],
    ["python3", "script.py"],
]


@pytest.mark.parametrize("argv", BLOCKED)
def test_blocked(argv):
    with pytest.raises(sg.WriteAttemptBlocked):
        sg.assert_read_only(argv)


@pytest.mark.parametrize("argv", ALLOWED)
def test_allowed(argv):
    sg.assert_read_only(argv)  # must not raise


# --- sanctioned bot-write path -------------------------------------------
BOT_OK = [
    ["gh", "pr", "comment", "123", "--body", "thanks"],
    ["gh", "pr", "close", "123"],
    ["gh", "pr", "reopen", "123"],
    ["gh", "pr", "review", "123", "--approve", "--body", "lgtm"],
    ["gh", "pr", "review", "123", "--request-changes", "--body", "please fix"],
    ["gh", "api", "repos/test-owner/test-repo/issues/123/comments", "-f", "body=x"],
    ["gh", "api", "--method", "POST", "repos/test-owner/test-repo/pulls/123/comments", "-f", "body=x"],
    ["gh", "api", "-X", "DELETE", "repos/test-owner/test-repo/issues/comments/456"],
    ["gh", "api", "--method", "DELETE", "repos/test-owner/test-repo/issues/comments/456"],
    # dismiss our own request-changes review on reopen (#70)
    ["gh", "api", "--method", "PUT", "repos/test-owner/test-repo/pulls/123/reviews/456/dismissals",
     "-f", "message=withdrawn", "-f", "event=DISMISS"],
]
BOT_BLOCKED = [
    ["gh", "pr", "merge", "123"],          # merge never allowed
    ["gh", "pr", "edit", "123"],
    ["gh", "issue", "create", "-t", "x"],  # not allowlisted
    ["git", "push"],                       # not gh
    ["gh", "pr", "diff", "123"],           # read op is not a bot-write op
    ["gh", "api", "-X", "DELETE", "repos/test-owner/test-repo/pulls/123"],  # DELETE only for comments
    ["gh", "api", "-X", "DELETE", "repos/test-owner/test-repo/issues/123"],  # not an issue itself
    ["gh", "api", "--method", "PUT", "repos/test-owner/test-repo/pulls/123/merge"],  # merge-by-API never allowed
    ["gh", "api", "--method", "PUT", "repos/test-owner/test-repo/pulls/123/reviews/456"],  # only /dismissals
]


@pytest.mark.parametrize("argv", BOT_OK)
def test_bot_write_allowed_shape(argv):
    sg.assert_bot_write(argv)  # shape ok


@pytest.mark.parametrize("argv", BOT_BLOCKED)
def test_bot_write_blocked_shape(argv):
    with pytest.raises(sg.WriteAttemptBlocked):
        sg.assert_bot_write(argv)


def test_bot_run_refuses_empty_token():
    # the load-bearing guarantee: no token → never write (would be default login)
    for bad in ("", "   ", None):
        with pytest.raises(sg.WriteAttemptBlocked):
            sg.bot_run(["gh", "pr", "close", "123"], bad)  # type: ignore[arg-type]


CHAT_BOT_OK = [
    ["gh", "pr", "comment", "123", "--body", "thanks"],
    ["gh", "pr", "edit", "123", "--title", "better title"],
    ["gh", "issue", "create", "--title", "bug", "--body", "details"],
    ["gh", "issue", "edit", "123", "--title", "better title"],
    ["gh", "issue", "reopen", "123"],
    ["gh", "run", "rerun", "456", "--failed"],
]


@pytest.mark.parametrize("argv", CHAT_BOT_OK)
def test_chat_bot_write_allowed_shape(argv):
    sg.assert_chat_bot_write(argv)


def test_chat_bot_run_refuses_empty_token():
    for bad in ("", "   ", None):
        with pytest.raises(sg.WriteAttemptBlocked):
            sg.chat_bot_run(["gh", "pr", "edit", "123", "--title", "x"], bad)  # type: ignore[arg-type]


# --- sanctioned bot-MERGE path (org-admin model) -------------------------
def test_bot_merge_refuses_empty_token():
    # merging as the configured bot still requires a real token — no token, no merge
    for bad in ("", "   ", None):
        with pytest.raises(sg.WriteAttemptBlocked):
            sg.bot_merge_run(["gh", "pr", "merge", "123", "--squash"], bad)  # type: ignore[arg-type]


def test_bot_merge_accepts_match_head_commit(monkeypatch):
    # pinning the merge to the verified head (--match-head-commit) is still a
    # sanctioned `gh pr merge <n>` — the guard must not reject the extra flag,
    # and it forwards the argv unchanged.
    from types import SimpleNamespace
    seen: dict = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sg.subprocess, "run", fake_run)
    argv = ["gh", "pr", "merge", "123", "--repo", "x/y", "--squash",
            "--match-head-commit", "abc123def"]
    sg.bot_merge_run(argv, "tok_realish")
    assert seen["argv"] == argv


def test_bot_merge_rejects_non_merge_even_with_token():
    # the merge path only ever runs `gh pr merge <n>` — nothing else
    for argv in (["gh", "pr", "close", "123"], ["gh", "pr", "edit", "123"],
                 ["gh", "api", "-X", "POST", "repos/x"], ["git", "push"]):
        with pytest.raises(sg.WriteAttemptBlocked):
            sg.bot_merge_run(argv, "tok_realish")


def test_merge_still_blocked_on_the_ordinary_paths():
    # defense in depth: `gh pr merge` is rejected by the read-only guard AND the
    # comment/close bot-write allowlist — it ONLY runs via bot_merge_run.
    with pytest.raises(sg.WriteAttemptBlocked):
        sg.assert_read_only(["gh", "pr", "merge", "5"])
    with pytest.raises(sg.WriteAttemptBlocked):
        sg.assert_bot_write(["gh", "pr", "merge", "5"])
