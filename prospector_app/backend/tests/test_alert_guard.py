"""The alert bot-write allowlist: exactly the three PATCH shapes, nothing else,
and never without a token."""
import pytest

from prospector_app.backend import safety_guard
from prospector_app.backend.safety_guard import WriteAttemptBlocked, assert_alert_bot_write


def _argv(endpoint: str) -> list[str]:
    return ["gh", "api", "-X", "PATCH", endpoint, "-f", "state=dismissed",
            "-f", "dismissed_reason=won't fix"]


def test_accepts_the_three_alert_endpoints():
    for src in ("code-scanning", "dependabot", "secret-scanning"):
        assert_alert_bot_write(_argv(f"repos/o/r/{src}/alerts/12"))


def test_accepts_method_flag_form():
    assert_alert_bot_write(["gh", "api", "--method", "PATCH",
                            "repos/o/r/dependabot/alerts/3", "-f", "state=dismissed"])


def test_refuses_other_verbs_and_endpoints():
    with pytest.raises(WriteAttemptBlocked):
        assert_alert_bot_write(["gh", "api", "-X", "DELETE", "repos/o/r/dependabot/alerts/3"])
    with pytest.raises(WriteAttemptBlocked):
        assert_alert_bot_write(["gh", "api", "-X", "PATCH", "repos/o/r/pulls/3"])
    with pytest.raises(WriteAttemptBlocked):
        assert_alert_bot_write(["gh", "pr", "merge", "3"])
    with pytest.raises(WriteAttemptBlocked):
        assert_alert_bot_write(["curl", "-X", "PATCH", "repos/o/r/dependabot/alerts/3"])


def test_alert_bot_run_refuses_empty_token():
    with pytest.raises(WriteAttemptBlocked, match="token"):
        safety_guard.alert_bot_run(_argv("repos/o/r/dependabot/alerts/3"), "")
    with pytest.raises(WriteAttemptBlocked, match="token"):
        safety_guard.alert_bot_run(_argv("repos/o/r/dependabot/alerts/3"), "   ")
