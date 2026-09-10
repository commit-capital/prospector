"""The agent's `gh-read` CLI — its GET-only window into GitHub raw contents +
code search. Driven end-to-end as a subprocess (how the sandboxed agent invokes
it) against a fake `gh` on PATH that echoes its argv, so the tests assert the
security-relevant invariant — the HTTP method is hardcoded to GET and the endpoint
is scoped to settings.repo() — without touching the network."""
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GH_READ = REPO_ROOT / "prospector_app" / "agent" / "gh-read"


def _fake_gh(bin_dir):
    """A `gh` on PATH that prints each argument on its own line, so a test can see
    exactly what argv the script built."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a"; done\n')
    gh.chmod(0o755)
    return bin_dir


def _run(bin_dir, args):
    # The sandboxed agent's invocation shape: a minimal env with the fake gh's dir
    # first on PATH, the identity vars settings.py requires, and TRIAGE_SKIP_DOTENV
    # so the subprocess never adopts a real .env — mirroring test_store_read_cli.
    return subprocess.run(
        [sys.executable, str(GH_READ), *args],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "TRIAGE_SKIP_DOTENV": "1",
             "TRIAGE_REPO": "test-owner/test-repo", "TRIAGE_BOT_LOGIN": "test-bot"},
        capture_output=True, text=True,
    )


def test_file_builds_a_get_against_the_repo_contents(tmp_path):
    r = _run(_fake_gh(tmp_path / "bin"), ["file", ".gitattributes"])
    assert r.returncode == 0, r.stderr
    argv = r.stdout.splitlines()
    assert argv[:2] == ["api", "repos/test-owner/test-repo/contents/.gitattributes"]
    assert "Accept: application/vnd.github.raw" in argv
    # GET-only: no write method is ever passed.
    assert "-X" not in argv


def test_file_ref_becomes_a_query_param(tmp_path):
    r = _run(_fake_gh(tmp_path / "bin"), ["file", "Dockerfile", "--ref", "abc123"])
    assert r.returncode == 0, r.stderr
    assert "repos/test-owner/test-repo/contents/Dockerfile?ref=abc123" in r.stdout.splitlines()


def test_leading_slash_in_path_stays_repo_scoped(tmp_path):
    # A leading slash must not make an absolute API path (escape the repo scope).
    r = _run(_fake_gh(tmp_path / "bin"), ["file", "/etc/passwd"])
    assert r.returncode == 0, r.stderr
    assert "repos/test-owner/test-repo/contents/etc/passwd" in r.stdout.splitlines()


def test_search_is_a_get_auto_scoped_to_the_repo(tmp_path):
    r = _run(_fake_gh(tmp_path / "bin"), ["search", "eol=lf"])
    assert r.returncode == 0, r.stderr
    argv = r.stdout.splitlines()
    assert argv[:4] == ["api", "-X", "GET", "search/code"]
    assert "q=eol=lf repo:test-owner/test-repo" in argv
    assert "per_page=30" in argv


def test_search_accepts_bounded_limit_and_jq_filter(tmp_path):
    r = _run(_fake_gh(tmp_path / "bin"), [
        "search", "eol=lf", "--limit", "500", "--jq", ".items[].path",
    ])
    assert r.returncode == 0, r.stderr
    argv = r.stdout.splitlines()
    assert "per_page=100" in argv
    assert argv[-2:] == ["--jq", ".items[].path"]
    assert argv[:4] == ["api", "-X", "GET", "search/code"]


def test_search_keeps_a_caller_supplied_repo_qualifier(tmp_path):
    r = _run(_fake_gh(tmp_path / "bin"), ["search", "dos2unix repo:other/repo"])
    assert r.returncode == 0, r.stderr
    # Not double-scoped: the caller's own repo: qualifier is left as-is.
    assert "q=dos2unix repo:other/repo" in r.stdout.splitlines()


def test_a_trailing_write_flag_is_rejected(tmp_path):
    # The whole point: there's no `-X PUT` escape. argparse rejects the unknown
    # flag before `gh` is ever called.
    r = _run(_fake_gh(tmp_path / "bin"), ["file", "foo", "-X", "PUT"])
    assert r.returncode != 0
    assert r.stdout == ""  # gh never ran


def test_commits_is_a_get_scoped_to_the_repo_path(tmp_path):
    r = _run(_fake_gh(tmp_path / "bin"), ["commits", "/server/src/routes/agents.ts", "--limit", "5"])
    assert r.returncode == 0, r.stderr
    argv = r.stdout.splitlines()
    assert argv[:4] == ["api", "-X", "GET", "repos/test-owner/test-repo/commits"]
    assert "path=server/src/routes/agents.ts" in argv and "per_page=5" in argv
    assert "PATCH" not in argv and "POST" not in argv and "PUT" not in argv


def test_commits_limit_is_clamped_to_github_page_size(tmp_path):
    r = _run(_fake_gh(tmp_path / "bin"), ["commits", "README.md", "--limit", "500"])
    assert "per_page=100" in r.stdout.splitlines()


def test_commit_is_a_plain_get_of_one_sha(tmp_path):
    r = _run(_fake_gh(tmp_path / "bin"), ["commit", "c647b8cc2ea6"])
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["api", "repos/test-owner/test-repo/commits/c647b8cc2ea6"]


def test_commit_accepts_jq_filter_without_changing_the_endpoint(tmp_path):
    r = _run(_fake_gh(tmp_path / "bin"), [
        "commit", "c647b8cc2ea6", "--jq", ".files[].filename",
    ])
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        "api", "repos/test-owner/test-repo/commits/c647b8cc2ea6",
        "--jq", ".files[].filename",
    ]


def test_commit_refuses_anything_but_a_sha(tmp_path):
    r = _run(_fake_gh(tmp_path / "bin"), ["commit", "../../rate_limit"])
    assert r.returncode == 2 and r.stdout == ""


def test_auth_runs_status_with_nothing_passed_through(tmp_path):
    r = _run(_fake_gh(tmp_path / "bin"), ["auth"])
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["auth", "status"]


def test_auth_refuses_a_show_token_flag(tmp_path):
    r = _run(_fake_gh(tmp_path / "bin"), ["auth", "--show-token"])
    assert r.returncode != 0
    assert r.stdout == ""  # gh never ran


def test_user_is_a_plain_get_of_one_login(tmp_path):
    r = _run(_fake_gh(tmp_path / "bin"), ["user", "octo-cat", "--jq", ".type"])
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["api", "users/octo-cat", "--jq", ".type"]


@pytest.mark.parametrize("login", ["../rate_limit", "-flag", "a b", "octo/cat", "", "-" * 40])
def test_user_refuses_anything_but_a_login(tmp_path, login):
    r = _run(_fake_gh(tmp_path / "bin"), ["user", login])
    assert r.returncode == 2 and r.stdout == ""
