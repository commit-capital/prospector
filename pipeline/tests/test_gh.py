"""gh transport helpers: run gh api and parse, degrading to None on failure.
subprocess.run is monkeypatched — no real network."""
import json
import subprocess
import types

from pipeline import gh


def _fake_run(stdout="", returncode=0):
    def run(argv, *, capture_output=True, text=True, timeout=60, env=None):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
    return run


def test_gh_json_parses_object(monkeypatch):
    monkeypatch.setattr(gh.subprocess, "run", _fake_run('{"a": 1}'))
    assert gh.gh_json("x") == {"a": 1}


def test_gh_json_none_on_array_body(monkeypatch):
    monkeypatch.setattr(gh.subprocess, "run", _fake_run('[1,2]'))
    assert gh.gh_json("x") is None


def test_gh_list_parses_array(monkeypatch):
    monkeypatch.setattr(gh.subprocess, "run", _fake_run('[{"n": 1}]'))
    assert gh.gh_list("x") == [{"n": 1}]


def test_gh_list_none_on_object_body(monkeypatch):
    monkeypatch.setattr(gh.subprocess, "run", _fake_run('{"a": 1}'))
    assert gh.gh_list("x") is None


def test_gh_api_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(gh.subprocess, "run", _fake_run("ignored", returncode=1))
    assert gh.gh_api("x") is None


def test_gh_api_none_on_bad_json(monkeypatch):
    monkeypatch.setattr(gh.subprocess, "run", _fake_run("not json"))
    assert gh.gh_api("x") is None


def test_gh_api_none_on_subprocess_error(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=60)
    monkeypatch.setattr(gh.subprocess, "run", boom)
    assert gh.gh_api("x") is None


def test_gh_api_uses_operator_login_environment(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "expired-app-token")
    monkeypatch.setenv("GITHUB_TOKEN", "expired-actions-token")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    seen = {}

    def run(argv, *, capture_output=True, text=True, timeout=60, env=None):
        seen["env"] = env
        return types.SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(gh.subprocess, "run", run)
    assert gh.gh_api("x") == {"ok": True}
    assert "GH_TOKEN" not in seen["env"]
    assert "GITHUB_TOKEN" not in seen["env"]


def test_operator_env_keeps_token_on_a_github_actions_runner(monkeypatch):
    # No keyring login exists on a fresh Actions runner — GH_TOKEN there is the
    # sanctioned, freshly-injected read identity, not a stale one (#92).
    monkeypatch.setenv("GH_TOKEN", "runner-token")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    env = gh.operator_env()
    assert env["GH_TOKEN"] == "runner-token"


def test_fetch_pr_uses_pulls_path(monkeypatch):
    seen = {}
    def run(argv, *, capture_output=True, text=True, timeout=60, env=None):
        seen["path"] = argv[2]
        return types.SimpleNamespace(returncode=0, stdout='{"number": 7}', stderr="")
    monkeypatch.setattr(gh.subprocess, "run", run)
    assert gh.fetch_pr(7) == {"number": 7}
    assert seen["path"].endswith("/pulls/7")


def test_check_runs_projects_and_dedupes(monkeypatch):
    payload = json.dumps({"check_runs": [
        {"name": "build", "conclusion": "success", "status": "completed", "extra": "x"},
        {"name": "build", "conclusion": "success", "status": "completed"},  # dup by (name, conclusion)
        {"name": "lint", "conclusion": "failure", "status": "completed"},
    ]})
    monkeypatch.setattr(gh.subprocess, "run", _fake_run(payload))
    assert gh.check_runs("abc") == [
        {"app": None, "name": "build", "conclusion": "success", "status": "completed",
         "title": None, "summary": None, "url": None},
        {"app": None, "name": "lint", "conclusion": "failure", "status": "completed",
         "title": None, "summary": None, "url": None},
    ]


def test_check_runs_empty_on_failure(monkeypatch):
    monkeypatch.setattr(gh.subprocess, "run", _fake_run("ignored", returncode=1))
    assert gh.check_runs("abc") == []


def test_check_runs_empty_on_falsy_sha(monkeypatch):
    called = {"n": 0}
    def run(argv, *, capture_output=True, text=True, timeout=60, env=None):
        called["n"] += 1
        return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")
    monkeypatch.setattr(gh.subprocess, "run", run)
    assert gh.check_runs("") == []
    assert called["n"] == 0   # no gh call for an empty sha


def test_gh_graphql_parses_object(monkeypatch):
    monkeypatch.setattr(gh.subprocess, "run", _fake_run('{"data": {"x": 1}}'))
    assert gh.gh_graphql("query {}") == {"data": {"x": 1}}


def test_gh_graphql_uses_operator_login_environment(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "expired-app-token")
    monkeypatch.setenv("GITHUB_TOKEN", "expired-actions-token")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    seen = {}

    def run(argv, *, capture_output=True, text=True, timeout=60, env=None):
        seen["env"] = env
        return types.SimpleNamespace(
            returncode=0, stdout='{"data": {"viewer": {"login": "operator"}}}', stderr="")

    monkeypatch.setattr(gh.subprocess, "run", run)
    assert gh.gh_graphql("query {}") == {"data": {"viewer": {"login": "operator"}}}
    assert "GH_TOKEN" not in seen["env"]
    assert "GITHUB_TOKEN" not in seen["env"]


def test_gh_graphql_none_on_nonzero_exit_with_unparseable_body(monkeypatch):
    monkeypatch.setattr(gh.subprocess, "run", _fake_run("ignored", returncode=1))
    assert gh.gh_graphql("query {}") is None


def test_gh_graphql_none_on_array_body(monkeypatch):
    monkeypatch.setattr(gh.subprocess, "run", _fake_run('[1, 2]'))
    assert gh.gh_graphql("query {}") is None


def test_gh_graphql_keeps_partial_data_on_nonzero_exit(monkeypatch):
    # gh exits 1 when the response carries any GraphQL error, yet still prints
    # the full envelope; errors coexist with partial data (one dead alias in a
    # batched query errors while every other alias resolves).
    envelope = json.dumps({
        "data": {"repository": {"p0": {"number": 1}, "p1": None}},
        "errors": [{"type": "NOT_FOUND",
                    "message": "Could not resolve to a PullRequest with the number of 10241."}],
    })
    monkeypatch.setattr(gh.subprocess, "run", _fake_run(envelope, returncode=1))
    out = gh.gh_graphql("query {}")
    assert out is not None
    assert out["data"]["repository"]["p0"] == {"number": 1}


def test_gh_graphql_none_on_nonzero_exit_without_data(monkeypatch):
    # A parseable error body with no data object (auth failure, rate limit) is
    # still a failed call.
    monkeypatch.setattr(
        gh.subprocess, "run",
        _fake_run('{"message": "API rate limit exceeded"}', returncode=1))
    assert gh.gh_graphql("query {}") is None
