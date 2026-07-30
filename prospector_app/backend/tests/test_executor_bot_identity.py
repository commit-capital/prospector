"""The REST API reports a GitHub App's actions under the `[bot]`-suffixed login
(`commitperclip[bot]`, `user.type == "Bot"`), while TRIAGE_BOT_LOGIN is the bare
App slug. The executor's read-back helpers (`_has_bot_comment`,
`_bot_comment_ids`, `_bot_change_request_ids`, `_latest_bot_review_url`) must
recognize the bot under either form — an exact-match on the bare login never
matches a REST payload, so comments re-post and reopen leaves the bot's closing
comment and standing request-changes review in place."""
import json
import types

from prospector_app.backend import executor


def _fake_run(monkeypatch, rows: list[dict]) -> list[list[str]]:
    """Stub `executor.run` to return `rows` as gh's `--jq` NDJSON (one compact
    JSON object per line — the shape a per-page `.[] | {…}` projection emits).
    Returns the captured argv list for endpoint assertions."""
    calls: list[list[str]] = []

    def fake(argv, timeout=30):
        calls.append(argv)
        out = "\n".join(json.dumps(r) for r in rows)
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(executor, "run", fake)
    return calls


# --- _is_bot_login -----------------------------------------------------------

def test_is_bot_login_accepts_bare_and_suffixed(monkeypatch):
    monkeypatch.setattr(executor, "BOT_LOGIN", "triagebot")
    assert executor._is_bot_login("triagebot")
    assert executor._is_bot_login("triagebot[bot]")


def test_is_bot_login_rejects_other_actors(monkeypatch):
    monkeypatch.setattr(executor, "BOT_LOGIN", "triagebot")
    assert not executor._is_bot_login("greptile-apps[bot]")
    assert not executor._is_bot_login("some-human")
    assert not executor._is_bot_login("triagebot2[bot]")  # prefix is not identity
    assert not executor._is_bot_login("")
    assert not executor._is_bot_login(None)


def test_is_bot_login_tolerates_suffixed_configuration(monkeypatch):
    # a deployment that sets TRIAGE_BOT_LOGIN with the suffix still matches both
    monkeypatch.setattr(executor, "BOT_LOGIN", "triagebot[bot]")
    assert executor._is_bot_login("triagebot")
    assert executor._is_bot_login("triagebot[bot]")


# --- _has_bot_comment --------------------------------------------------------

def test_has_bot_comment_matches_suffixed_rest_login(monkeypatch):
    monkeypatch.setattr(executor, "BOT_LOGIN", "triagebot")
    calls = _fake_run(monkeypatch, [
        {"login": "some-human", "body": "thanks!"},
        {"login": "triagebot[bot]", "body": "Closing as duplicate of #12."},
    ])
    assert executor._has_bot_comment(101) is True
    assert any(f"issues/101/comments" in a for a in calls[0])


def test_has_bot_comment_contains_scopes_to_bot_comments_only(monkeypatch):
    monkeypatch.setattr(executor, "BOT_LOGIN", "triagebot")
    _fake_run(monkeypatch, [
        {"login": "some-human", "body": "Closing as duplicate of #12."},
        {"login": "triagebot[bot]", "body": "re-triggered review"},
    ])
    # the human's body matches the key, the bot's doesn't → no bot comment counts
    assert executor._has_bot_comment(101, "Closing as duplicate") is False


def test_has_bot_comment_false_when_no_bot_comment(monkeypatch):
    monkeypatch.setattr(executor, "BOT_LOGIN", "triagebot")
    _fake_run(monkeypatch, [{"login": "some-human", "body": "hi"}])
    assert executor._has_bot_comment(101) is False


def test_has_bot_comment_false_on_garbage_output(monkeypatch):
    monkeypatch.setattr(executor, "BOT_LOGIN", "triagebot")
    monkeypatch.setattr(executor, "run", lambda argv, timeout=30: types.SimpleNamespace(
        returncode=1, stdout="gh: Not Found", stderr=""))
    assert executor._has_bot_comment(101) is False


# --- _bot_comment_ids --------------------------------------------------------

def test_bot_comment_ids_filters_by_tolerant_login(monkeypatch):
    monkeypatch.setattr(executor, "BOT_LOGIN", "triagebot")
    calls = _fake_run(monkeypatch, [
        {"login": "triagebot[bot]", "id": 11},
        {"login": "greptile-apps[bot]", "id": 22},
        {"login": "triagebot", "id": 33},
    ])
    assert executor._bot_comment_ids(101) == [11, 33]
    assert any("issues/101/comments" in a for a in calls[0])


def test_bot_comment_ids_empty_on_error(monkeypatch):
    monkeypatch.setattr(executor, "BOT_LOGIN", "triagebot")
    monkeypatch.setattr(executor, "run", lambda argv, timeout=30: types.SimpleNamespace(
        returncode=1, stdout="", stderr="boom"))
    assert executor._bot_comment_ids(101) == []


# --- _bot_change_request_ids -------------------------------------------------

def test_bot_change_request_ids_filters_login_and_state(monkeypatch):
    monkeypatch.setattr(executor, "BOT_LOGIN", "triagebot")
    calls = _fake_run(monkeypatch, [
        {"login": "triagebot[bot]", "id": 1, "state": "COMMENTED"},
        {"login": "coderabbitai[bot]", "id": 2, "state": "CHANGES_REQUESTED"},
        {"login": "triagebot[bot]", "id": 3, "state": "CHANGES_REQUESTED"},
    ])
    assert executor._bot_change_request_ids(101) == [3]
    assert any("pulls/101/reviews" in a for a in calls[0])


def test_bot_change_request_ids_empty_on_error(monkeypatch):
    monkeypatch.setattr(executor, "BOT_LOGIN", "triagebot")
    monkeypatch.setattr(executor, "run", lambda argv, timeout=30: types.SimpleNamespace(
        returncode=1, stdout="", stderr="boom"))
    assert executor._bot_change_request_ids(101) == []


# --- _latest_bot_review_url --------------------------------------------------

def test_latest_bot_review_url_returns_last_bot_review(monkeypatch):
    monkeypatch.setattr(executor, "BOT_LOGIN", "triagebot")
    _fake_run(monkeypatch, [
        {"login": "triagebot[bot]", "url": "https://gh/r/1"},
        {"login": "greptile-apps[bot]", "url": "https://gh/r/2"},
        {"login": "triagebot[bot]", "url": "https://gh/r/3"},
    ])
    assert executor._latest_bot_review_url(101) == "https://gh/r/3"


def test_latest_bot_review_url_none_when_no_bot_review(monkeypatch):
    monkeypatch.setattr(executor, "BOT_LOGIN", "triagebot")
    _fake_run(monkeypatch, [{"login": "greptile-apps[bot]", "url": "https://gh/r/2"}])
    assert executor._latest_bot_review_url(101) is None
