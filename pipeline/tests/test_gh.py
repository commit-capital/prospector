"""gh transport helpers: run gh api and parse, degrading to None on failure.
subprocess.run is monkeypatched — no real network."""
import json
import subprocess
import types

from pipeline import gh


def _fake_run(stdout="", returncode=0):
    def run(argv, *, capture_output=True, text=True, timeout=60):
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


def test_fetch_pr_uses_pulls_path(monkeypatch):
    seen = {}
    def run(argv, *, capture_output=True, text=True, timeout=60):
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
        {"name": "build", "conclusion": "success", "status": "completed"},
        {"name": "lint", "conclusion": "failure", "status": "completed"},
    ]


def test_check_runs_empty_on_failure(monkeypatch):
    monkeypatch.setattr(gh.subprocess, "run", _fake_run("ignored", returncode=1))
    assert gh.check_runs("abc") == []


def test_check_runs_empty_on_falsy_sha(monkeypatch):
    called = {"n": 0}
    def run(argv, *, capture_output=True, text=True, timeout=60):
        called["n"] += 1
        return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")
    monkeypatch.setattr(gh.subprocess, "run", run)
    assert gh.check_runs("") == []
    assert called["n"] == 0   # no gh call for an empty sha


def test_gh_graphql_parses_object(monkeypatch):
    monkeypatch.setattr(gh.subprocess, "run", _fake_run('{"data": {"x": 1}}'))
    assert gh.gh_graphql("query {}") == {"data": {"x": 1}}


def test_gh_graphql_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(gh.subprocess, "run", _fake_run("ignored", returncode=1))
    assert gh.gh_graphql("query {}") is None


def test_gh_graphql_none_on_array_body(monkeypatch):
    monkeypatch.setattr(gh.subprocess, "run", _fake_run('[1, 2]'))
    assert gh.gh_graphql("query {}") is None
