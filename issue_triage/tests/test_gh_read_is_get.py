"""Regression: gh_read must force `-X GET` so `gh api -f` never flips to POST
(an accidental write against test-owner/test-repo). See config.gh_read."""
from unittest import mock
from issue_triage import config


def test_gh_read_forces_get():
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return mock.Mock(stdout="[]")

    with mock.patch.object(config.subprocess, "run", side_effect=fake_run):
        config.gh_read("repos/x/y/issues", {"state": "open"})

    cmd = captured["cmd"]
    assert "-X" in cmd and cmd[cmd.index("-X") + 1] == "GET"
    # and no write verb ever
    assert not any(v in cmd for v in ("POST", "PATCH", "DELETE", "PUT"))


def test_gh_graphql_sends_string_fields():
    """Variables go through `-f` (raw string), not `-F` (typed): `-F` would coerce
    an all-digit cursor to an int against the query's String variable. See
    config.gh_graphql."""
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return mock.Mock(stdout='{"data": {}}')

    with mock.patch.object(config.subprocess, "run", side_effect=fake_run):
        config.gh_graphql("query { viewer { login } }", {"cursor": "123"})

    cmd = captured["cmd"]
    assert "graphql" in cmd
    assert "-F" not in cmd  # typed fields would mis-detect string-typed variables
    assert "cursor=123" in cmd and cmd[cmd.index("cursor=123") - 1] == "-f"
    assert not any(v in cmd for v in ("POST", "PATCH", "DELETE", "PUT"))
