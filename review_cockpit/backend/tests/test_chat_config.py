"""Guards on how the cockpit chat agent is sandboxed (chat.isolation_flags).

These are the security-relevant invariants of the embedded assistant: isolation
from the operator's harness, dontAsk, a read `gh` allowlist, and the curated
upstream-write commands that are unlocked ONLY when a bot token is
available (writes go out as the bot, never as the operator's login).
"""
import os

import pytest

from pipeline import review_policy
from pipeline import settings
from review_cockpit.backend import chat


def _flag(flags, name):
    i = flags.index(name)
    return flags[i + 1]


def _multi(flags, name):
    """The run of values after a flag that spreads several argv entries (e.g.
    --disallowedTools Task Edit …), up to the next --flag."""
    i = flags.index(name) + 1
    out = []
    while i < len(flags) and not flags[i].startswith("--"):
        out.append(flags[i])
        i += 1
    return out


def test_runs_in_dontask_not_plan():
    # plan mode is what produced the "permissions / plan" chatter; dontAsk
    # silently denies anything outside the allowlist.
    assert _flag(chat.isolation_flags(False), "--permission-mode") == "dontAsk"


def test_safe_mode_isolates_from_dev_harness():
    # --safe-mode disables CLAUDE.md (no double duty with the dev manual), hooks,
    # plugins, skills, MCP servers, and custom agents/commands. True regardless of
    # whether writes are unlocked.
    assert "--safe-mode" in chat.isolation_flags(False)
    assert "--safe-mode" in chat.isolation_flags(True)


def test_no_settings_files_are_loaded():
    # --setting-sources "" loads NO settings file: safe-mode leaves permissions
    # working normally, so the project `.claude/settings.json` deny rules would
    # otherwise apply to this agent too and block its own sanctioned bot writes
    # (#374). Dropping the settings files leaves the agent's boundary as its
    # dontAsk + CLI allowlist.
    for token in (False, True):
        flags = chat.isolation_flags(token)
        assert _flag(flags, "--setting-sources") == ""


def test_agent_has_its_own_context_not_the_dev_claude_md(monkeypatch):
    # The agent's operating manual is a dedicated file under agent/, loaded into
    # its prompt — not the dev CLAUDE.md.
    assert chat.AGENT_CONTEXT.name == "context.md"
    assert chat.AGENT_CONTEXT.parent.name == "agent"
    monkeypatch.setattr(chat, "DISPLAY_NAME", "Example Project")
    sp = chat.system_prompt()
    assert "Example Project Prospector" in sp
    assert "{display_name}" not in sp
    assert "store-read pr" in sp  # the real doc, pointing at the store accessor


def test_missing_context_is_a_loud_failure(monkeypatch, tmp_path):
    # No silent fallback: if the manual is gone, refuse rather than run the
    # triage agent on a degraded prompt.
    monkeypatch.setattr(chat, "AGENT_CONTEXT", tmp_path / "nope.md")
    with pytest.raises(RuntimeError):
        chat.system_prompt()
    empty = tmp_path / "empty.md"
    empty.write_text("   \n")
    monkeypatch.setattr(chat, "AGENT_CONTEXT", empty)
    with pytest.raises(RuntimeError):
        chat.system_prompt()


def test_read_only_allowlist_without_a_token():
    # No bot token → the read subcommands and none of the upstream writes.
    allowed = _flag(chat.isolation_flags(False), "--allowedTools")
    for sub in ("gh pr view", "gh pr diff", "gh pr list", "gh pr checks",
                "gh pr status", "gh issue view", "gh issue list",
                "gh search prs", "gh search issues", "gh search commits",
                "gh release view", "gh run view"):
        assert f"Bash({sub}:*)" in allowed
    # no upstream write is even advertised without a token — `gh issue create`
    # (upstream filing, as the bot) among them.
    for danger in ("gh pr edit", "gh pr comment", "gh pr close", "gh pr reopen",
                   "gh pr review", "gh pr update-branch", "gh pr merge", "gh api",
                   "gh issue create", "gh issue close", "gh issue reopen",
                   "gh issue comment", "gh issue edit", "gh run rerun"):
        assert danger not in allowed
    # resubmit (push-as-the-operator) and the Edit/Write tools it needs are NOT
    # offered without a token, and Edit/Write stay on the effective deny list —
    # a read-only machine can't touch files on disk or push a branch.
    flags = chat.isolation_flags(False)
    assert "review_cockpit/agent/resubmit" not in allowed
    assert "Edit" not in allowed and "Write" not in allowed
    assert "Edit" in _multi(flags, "--disallowedTools")
    assert "Write" in _multi(flags, "--disallowedTools")


def test_resubmit_and_file_edits_unlocked_with_a_token():
    # On a real operator machine (token) the resubmit path is offered, and the
    # Edit/Write tools it needs to author the change are lifted off the deny list.
    # This is the ONE operator-identity write; the push itself runs as the
    # operator (the `resubmit` helper drops the bot token), not the bot.
    flags = chat.isolation_flags(True)
    allowed = _flag(flags, "--allowedTools")
    assert "Bash(review_cockpit/agent/resubmit:*)" in allowed
    assert "Edit" in allowed and "Write" in allowed
    disallowed = _multi(flags, "--disallowedTools")
    assert "Edit" not in disallowed and "Write" not in disallowed
    # NotebookEdit is never needed and stays denied.
    assert "NotebookEdit" in disallowed
    # the real, executable script backs the allowlisted path.
    script = chat.COCKPIT / "agent" / "resubmit"
    assert script.exists() and os.access(script, os.X_OK)


def test_curated_writes_unlocked_with_a_token_but_never_merge():
    # With a token the curated upstream writes are added on top of the reads...
    allowed = _flag(chat.isolation_flags(True), "--allowedTools")
    for sub in ("gh pr edit", "gh pr comment", "gh pr close", "gh pr reopen", "gh pr review",
                "gh issue create", "gh issue close", "gh issue reopen",
                "gh issue comment", "gh issue edit", "gh run rerun"):
        assert f"Bash({sub}:*)" in allowed
    # ...the reads are still present...
    assert "Bash(gh pr view:*)" in allowed
    # ...but merge is NEVER unlocked here (it stays on the executor's gated path),
    # and there is no raw `gh api` escape hatch.
    assert "gh pr merge" not in allowed
    assert "gh api" not in allowed
    # Updating a stale PR's branch is not a bot write either: an App token can't
    # write the `.github/workflows/**` files a moved base branch carries into the
    # merge, so it runs as the operator through `resubmit <pr> update`.
    assert "gh pr update-branch" not in allowed
    assert "Bash(review_cockpit/agent/resubmit:*)" in allowed


def test_local_self_writes_are_allowlisted_and_executable():
    # The agent's local self-writes (no upstream, no token): persist a learning to
    # its memory, and detach a mis-grouped PR from a cluster. Both ride the
    # always-on allowlisted-Bash path, present even with no token.
    for token in (False, True):
        allowed = _flag(chat.isolation_flags(token), "--allowedTools")
        assert "Bash(review_cockpit/agent/remember:*)" in allowed
        assert "Bash(review_cockpit/agent/uncluster:*)" in allowed
    # Each allowlisted path must be the real, executable script.
    for name in ("remember", "uncluster"):
        script = chat.COCKPIT / "agent" / name
        assert script.exists() and os.access(script, os.X_OK)


def test_file_issue_is_allowlisted_and_executable():
    # Filing a triager bug on the meta-repo runs as the OPERATOR through its own
    # script (the meta-repo is outside the bot's app installation, so a
    # bot-authenticated `gh` can't resolve it). Available with or without a token —
    # a tooling bug is reportable on every machine.
    for token in (False, True):
        allowed = _flag(chat.isolation_flags(token), "--allowedTools")
        assert "Bash(review_cockpit/agent/file-issue:*)" in allowed
    script = chat.COCKPIT / "agent" / "file-issue"
    assert script.exists() and os.access(script, os.X_OK)


def test_store_read_is_allowlisted_and_executable():
    # The agent's read window into the SQL store rides the same always-on
    # allowlisted-Bash path (no upstream, no token) — present with or without one.
    for token in (False, True):
        allowed = _flag(chat.isolation_flags(token), "--allowedTools")
        assert "Bash(review_cockpit/agent/store-read:*)" in allowed
    script = chat.COCKPIT / "agent" / "store-read"
    assert script.exists() and os.access(script, os.X_OK)


def test_reingest_is_allowlisted_and_executable():
    # The agent's "refresh one PR" path (a local store edit, no upstream/token) rides
    # the same always-on allowlisted-Bash path — present with or without a token.
    for token in (False, True):
        allowed = _flag(chat.isolation_flags(token), "--allowedTools")
        assert "Bash(review_cockpit/agent/reingest:*)" in allowed
    script = chat.COCKPIT / "agent" / "reingest"
    assert script.exists() and os.access(script, os.X_OK)


def test_gh_read_is_allowlisted_and_executable():
    # The agent's GET-only window into GitHub raw contents + code search (no
    # upstream write, no token) rides the same always-on allowlisted-Bash path —
    # present with or without a token.
    for token in (False, True):
        allowed = _flag(chat.isolation_flags(token), "--allowedTools")
        assert "Bash(review_cockpit/agent/gh-read:*)" in allowed
    script = chat.COCKPIT / "agent" / "gh-read"
    assert script.exists() and os.access(script, os.X_OK)


def test_git_diff_is_allowlisted_read_only():
    # `git diff` is read-only, so it's on the always-on allowlist (with or without a
    # token) — but only that subcommand; mutating git commands stay denied.
    for token in (False, True):
        allowed = _flag(chat.isolation_flags(token), "--allowedTools")
        assert "Bash(git diff:*)" in allowed
        for danger in ("git commit", "git push", "git checkout", "git reset"):
            assert danger not in allowed


def test_write_and_agentic_tools_are_disallowed_but_bash_is_not():
    # _DISALLOWED_TOOLS is the read-only baseline. Edit/Write sit here by default
    # (lifted only with a token, for resubmit — see the test above); the agentic
    # tools are denied unconditionally.
    for t in ("Write", "Edit", "Task", "Workflow", "Skill", "WebFetch"):
        assert t in chat._DISALLOWED_TOOLS
    # the agentic tools are never lifted, even on an operator machine.
    for t in ("Task", "Workflow", "Skill", "WebFetch"):
        assert t in _multi(chat.isolation_flags(True), "--disallowedTools")
    # Bash stays available — it's the carrier for the gh allowlist.
    assert "Bash" not in chat._DISALLOWED_TOOLS


def test_bot_token_is_cached_and_none_stays_read_only(monkeypatch):
    # _bot_token caches a minted token (so we don't re-mint a shell-out + two API
    # calls every chat turn) and returns None when minting fails — which keeps the
    # agent read-only.
    monkeypatch.setattr(chat, "_BOT_TOKEN", None)
    monkeypatch.setattr(chat, "_BOT_TOKEN_EXP", 0.0)
    calls = {"n": 0}

    def fake_mint():
        calls["n"] += 1
        return "tok-123"

    monkeypatch.setattr(chat.executor, "mint_bot_token", fake_mint)
    assert chat._bot_token() == "tok-123"
    assert chat._bot_token() == "tok-123"  # served from cache
    assert calls["n"] == 1                 # minted exactly once

    # a failed mint → None (agent then runs read-only, no write tools offered).
    monkeypatch.setattr(chat, "_BOT_TOKEN", None)
    monkeypatch.setattr(chat, "_BOT_TOKEN_EXP", 0.0)
    monkeypatch.setattr(chat.executor, "mint_bot_token", lambda: None)
    assert chat._bot_token() is None


def test_context_has_critic_and_issue_guidance():
    sp = chat.system_prompt()
    # two-pass-blind discipline
    assert "form your own" in sp.lower()
    assert "before you read" in sp.lower()
    # issue-filing capability + target repo (the configured feedback repo,
    # substituted for {feedback_repo} — conftest pins test-owner/test-meta-repo)
    assert "gh issue create" in sp
    assert "test-owner/test-meta-repo" in sp
    assert "{feedback_repo}" not in sp
    # both failure modes named
    assert "clustering" in sp.lower() and "disposition" in sp.lower()


def test_context_documents_upstream_writes_and_the_merge_limit():
    sp = chat.system_prompt()
    # The prompt interpolates the configured deployment identity and repo.
    assert chat.BOT_LOGIN in sp
    assert chat.REPO in sp
    assert "{bot}" not in sp and "{repo}" not in sp
    assert "gh pr edit" in sp
    assert "gh run rerun" in sp
    # Updating a stale PR's branch is documented as the operator-identity path, so
    # the agent doesn't look for a bot command that isn't allowlisted.
    assert "resubmit <pr> update" in sp
    assert "gh pr update-branch" not in sp
    # ...and the hard "never merge" limit is stated.
    assert "merge" in sp.lower()
    assert "cannot" in sp.lower() or "never" in sp.lower()


def test_context_documents_the_review_retrigger(monkeypatch):
    # Re-triggering the review is a bot comment carrying the provider's mention —
    # the prompt names the configured one so the agent doesn't have to guess it.
    sp = chat.system_prompt()
    assert "gh pr comment" in sp
    assert review_policy.active().retrigger_mention == "@greptileai"
    assert "@greptileai" in sp
    assert "{retrigger_mention}" not in sp
    # A deployment whose provider has no re-trigger says so, rather than leaving a
    # bare placeholder for the agent to invent a mention from.
    monkeypatch.setattr(settings, "REVIEW_PROVIDER", "none")
    sp = chat.system_prompt()
    assert "@greptileai" not in sp
    assert "{retrigger_mention}" not in sp
