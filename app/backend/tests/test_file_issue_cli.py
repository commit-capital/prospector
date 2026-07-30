"""The agent's `file-issue` CLI — its meta-repo bug-filing write. Driven end-to-end
as a subprocess (how the sandboxed agent actually invokes it) against a stub `gh`
on PATH, so the test asserts the two things that matter — the repo is pinned to the
configured meta-repo, and the bot token is dropped so the issue is filed under the
operator's own login — without touching GitHub."""
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
FILE_ISSUE = REPO_ROOT / "app" / "agent" / "file-issue"

# A stub `gh` that records its argv and whether a token reached it, then prints the
# issue URL the real command would.
_STUB_GH = """#!/usr/bin/env python3
import json, os, sys
json.dump({"argv": sys.argv[1:], "gh_token": os.environ.get("GH_TOKEN"),
           "github_token": os.environ.get("GITHUB_TOKEN")},
          open(os.environ["STUB_GH_LOG"], "w"))
print("https://github.com/test-owner/test-meta-repo/issues/42")
"""


def _run(tmp_path, args, feedback_repo="test-owner/test-meta-repo"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(_STUB_GH)
    gh.chmod(0o755)
    log = tmp_path / "gh.json"
    # The sandboxed agent's invocation shape: a bot GH_TOKEN in the env (what makes
    # the meta-repo unreachable), plus the identity vars settings.py requires and
    # TRIAGE_SKIP_DOTENV so the subprocess stays hermetic against any real .env.
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "TRIAGE_SKIP_DOTENV": "1",
           "TRIAGE_REPO": "test-owner/test-repo", "TRIAGE_BOT_LOGIN": "test-bot",
           "COCKPIT_FEEDBACK_REPO": feedback_repo,
           "GH_TOKEN": "bot-token", "GITHUB_TOKEN": "bot-token",
           "STUB_GH_LOG": str(log)}
    r = subprocess.run([sys.executable, str(FILE_ISSUE), *args],
                       env=env, capture_output=True, text=True)
    call = json.loads(log.read_text()) if log.exists() else None
    return r, call


def test_files_on_the_meta_repo_as_the_operator(tmp_path):
    r, call = _run(tmp_path, ["--title", "clustering is off", "--body", "the details",
                              "--label", "bug"])
    assert r.returncode == 0, r.stderr
    assert "issues/42" in r.stdout
    assert call is not None
    # the repo is pinned to the configured meta-repo...
    assert call["argv"][:3] == ["issue", "create", "--repo"]
    assert call["argv"][3] == "test-owner/test-meta-repo"
    assert "--title" in call["argv"] and "clustering is off" in call["argv"]
    assert "--label" in call["argv"] and "bug" in call["argv"]
    # ...and the bot token is gone, so `gh` runs under the operator's own login.
    assert call["gh_token"] is None
    assert call["github_token"] is None


def test_body_file_is_passed_through(tmp_path):
    body = tmp_path / "body.md"
    body.write_text("a long write-up")
    r, call = _run(tmp_path, ["--title", "t", "--body-file", str(body)])
    assert r.returncode == 0, r.stderr
    assert call is not None
    assert call["argv"][call["argv"].index("--body-file") + 1] == str(body)


def test_missing_body_file_refuses_before_calling_gh(tmp_path):
    r, call = _run(tmp_path, ["--title", "t", "--body-file", str(tmp_path / "nope.md")])
    assert r.returncode == 2
    assert "body file not found" in r.stderr
    assert call is None


def test_unconfigured_meta_repo_refuses(tmp_path):
    r, call = _run(tmp_path, ["--title", "t", "--body", "b"], feedback_repo="")
    assert r.returncode == 2
    assert "COCKPIT_FEEDBACK_REPO" in r.stderr
    assert call is None


def test_the_repo_cannot_be_overridden(tmp_path):
    # No `--repo` flag exists: the operator-identity write reaches the meta-repo and
    # nothing else, so an attempt to retarget it fails at argument parsing.
    r, call = _run(tmp_path, ["--repo", "test-owner/test-repo", "--title", "t",
                              "--body", "b"])
    assert r.returncode != 0
    assert call is None
