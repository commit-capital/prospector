"""The agent's `file-issue` CLI — its meta-repo bug-filing write. Driven end-to-end
as a subprocess (how the sandboxed agent actually invokes it) against a stub `gh`
on PATH, so the test asserts the two things that matter — the repo is pinned to the
configured meta-repo, and the bot token is dropped so the issue is filed under the
operator's own login — without touching GitHub."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FILE_ISSUE = REPO_ROOT / "prospector_app" / "agent" / "file-issue"

# A stub `gh` that records its argv and whether a token reached it, then prints the
# issue URL the real command would, or fails the way GitHub refuses a credential.
_STUB_GH = """#!/usr/bin/env python3
import json, os, sys
json.dump({"argv": sys.argv[1:], "gh_token": os.environ.get("GH_TOKEN"),
           "github_token": os.environ.get("GITHUB_TOKEN")},
          open(os.environ["STUB_GH_LOG"], "w"))
if os.environ.get("STUB_GH_FAIL"):
    print("GraphQL: Resource not accessible by personal access token (createIssue)",
          file=sys.stderr)
    sys.exit(1)
print(os.environ.get("STUB_GH_OUTPUT", "https://github.com/test-owner/test-meta-repo/issues/42"))
"""


def _run(tmp_path, args, feedback_repo="test-owner/test-meta-repo", gh_output=None,
         feedback_token=None, gh_fails=False):
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
           "PROSPECTOR_FEEDBACK_REPO": feedback_repo,
           "GH_TOKEN": "bot-token", "GITHUB_TOKEN": "bot-token",
           "STUB_GH_LOG": str(log)}
    if gh_output is not None:
        env["STUB_GH_OUTPUT"] = gh_output
    if feedback_token is not None:
        env["PROSPECTOR_FEEDBACK_TOKEN"] = feedback_token
    if gh_fails:
        env["STUB_GH_FAIL"] = "1"
    r = subprocess.run([sys.executable, str(FILE_ISSUE), *args],
                       env=env, capture_output=True, text=True)
    call = json.loads(log.read_text()) if log.exists() else None
    return r, call


def test_files_on_the_meta_repo_as_the_operator(tmp_path):
    r, call = _run(tmp_path, ["--title", "clustering is off", "--body", "the details",
                              "--label", "bug"])
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {
        "ok": True,
        "kind": "feedback-issue",
        "repo": "test-owner/test-meta-repo",
        "number": 42,
        "url": "https://github.com/test-owner/test-meta-repo/issues/42",
    }
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
    assert "PROSPECTOR_FEEDBACK_REPO" in r.stderr
    assert call is None


def test_the_repo_cannot_be_overridden(tmp_path):
    # No `--repo` flag exists: the operator-identity write reaches the meta-repo and
    # nothing else, so an attempt to retarget it fails at argument parsing.
    r, call = _run(tmp_path, ["--repo", "test-owner/test-repo", "--title", "t",
                              "--body", "b"])
    assert r.returncode != 0
    assert call is None


def test_unexpected_gh_success_output_is_not_a_receipt(tmp_path):
    r, call = _run(tmp_path, ["--title", "t", "--body", "b"],
                   gh_output="Issue created, probably #42")
    assert call is not None
    assert r.returncode == 1
    assert r.stdout == ""
    assert "unexpected success output" in r.stderr


def test_a_feedback_token_files_as_its_own_credential(tmp_path):
    r, call = _run(tmp_path, ["--title", "t", "--body", "b"],
                   feedback_token="github_pat_feedback")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["number"] == 42
    assert call is not None
    assert call["gh_token"] == "github_pat_feedback"
    assert call["github_token"] is None


def test_surrounding_whitespace_is_trimmed_from_the_feedback_token(tmp_path):
    r, call = _run(tmp_path, ["--title", "t", "--body", "b"],
                   feedback_token="  github_pat_feedback\n")
    assert r.returncode == 0, r.stderr
    assert call is not None
    assert call["gh_token"] == "github_pat_feedback"


@pytest.mark.parametrize("blank", ["", " ", "\t\n"])
def test_a_blank_feedback_token_reads_as_unset(tmp_path, blank):
    r, call = _run(tmp_path, ["--title", "t", "--body", "b"], feedback_token=blank)
    assert r.returncode == 0, r.stderr
    assert call is not None
    assert call["gh_token"] is None
    assert call["github_token"] is None


def test_a_refusal_under_the_stored_login_names_the_repo_identity_and_grant(tmp_path):
    r, call = _run(tmp_path, ["--title", "t", "--body", "b"], gh_fails=True)
    assert call is not None
    assert r.returncode == 1
    assert r.stdout == ""
    assert "Resource not accessible by personal access token" in r.stderr
    assert "test-owner/test-meta-repo" in r.stderr
    assert "gh's stored login" in r.stderr
    assert "PROSPECTOR_FEEDBACK_TOKEN" in r.stderr
    assert "Issues: Read and write" in r.stderr
    assert "resource owner" in r.stderr and "test-owner" in r.stderr


def test_a_refusal_under_a_fine_grained_feedback_token_names_its_resource_owner(tmp_path):
    r, call = _run(tmp_path, ["--title", "t", "--body", "b"],
                   feedback_token="github_pat_feedback", gh_fails=True)
    assert call is not None
    assert r.returncode == 1
    assert "test-owner/test-meta-repo" in r.stderr
    assert "as PROSPECTOR_FEEDBACK_TOKEN" in r.stderr
    assert "gh's stored login" not in r.stderr
    assert "Issues: Read and write" in r.stderr
    assert "resource owner" in r.stderr
    assert "github_pat_feedback" not in r.stderr


def test_a_refusal_under_a_classic_feedback_token_omits_the_resource_owner(tmp_path):
    r, call = _run(tmp_path, ["--title", "t", "--body", "b"],
                   feedback_token="ghp_classic", gh_fails=True)
    assert call is not None
    assert r.returncode == 1
    assert "as PROSPECTOR_FEEDBACK_TOKEN" in r.stderr
    assert "Issues: Read and write" in r.stderr
    assert "resource owner" not in r.stderr
    assert "ghp_classic" not in r.stderr
