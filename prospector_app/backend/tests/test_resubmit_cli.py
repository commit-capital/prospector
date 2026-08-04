"""The agent's `resubmit` helper — author a change on a contributor's fork branch
and push it AS THE CONFIGURED MACHINE USER. The git/gh mechanics need a live
fork, so the unit tests here lock down the safety-critical PURE logic: the
push-eligibility preflight, the machine-user env swap (drop the App token, pin
the key, stamp the commit identity), the push-target fence, the fork ssh URL,
and the argparse surface. The module has no `.py` extension (it runs under the
sandbox's bare python3), so we load it by path."""
import importlib.util
import json
import os
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RESUBMIT_PATH = REPO_ROOT / "prospector_app" / "agent" / "resubmit"


def _load():
    # The script has no `.py` suffix (it runs under the sandbox's bare python3),
    # so name its loader explicitly rather than inferring from the extension.
    loader = SourceFileLoader("resubmit_cli", str(RESUBMIT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


resubmit = _load()


@pytest.fixture(autouse=True)
def push_identity(monkeypatch, tmp_path_factory):
    """Every push path refuses without a configured machine user, so the suite
    configures one. The key is a real file because push_env stats it."""
    key = tmp_path_factory.mktemp("push-key") / "id_ed25519"
    key.write_text("not-a-real-key\n")
    monkeypatch.setattr(resubmit.settings, "PUSH_LOGIN", "test-push-bot")
    monkeypatch.setattr(resubmit.settings, "PUSH_EMAIL",
                        "1+test-push-bot@users.noreply.github.com")
    monkeypatch.setattr(resubmit.settings, "PUSH_SSH_KEY_FILE", key)
    return key


# --- eligibility: the preflight that decides whether a push is even possible ----

def _pr(**over):
    base = {"number": 42, "state": "OPEN", "baseRefName": "master", "headRefName": "fix",
            "baseRefOid": "c" * 40, "headRefOid": "a" * 40, "isCrossRepository": True,
            "maintainerCanModify": True,
            "headRepository": {"nameWithOwner": "contrib/test-repo"},
            "headRepositoryOwner": {"login": "contrib"}}
    base.update(over)
    return base


def test_eligible_when_cross_repo_and_maintainer_can_modify():
    ok, reason = resubmit.eligibility(_pr())
    assert ok is True
    assert "maintainers" in reason.lower()


def test_ineligible_when_maintainer_edits_off():
    ok, reason = resubmit.eligibility(_pr(maintainerCanModify=False))
    assert ok is False
    assert "off" in reason.lower()  # tells the operator why, so it can relay it


def test_eligible_same_repo_branch_even_without_maintainer_flag():
    # A branch on the upstream repo itself: the operator has direct push access,
    # so maintainerCanModify is irrelevant.
    ok, _ = resubmit.eligibility(_pr(isCrossRepository=False, maintainerCanModify=False))
    assert ok is True


@pytest.mark.parametrize("state", ["CLOSED", "MERGED", ""])
def test_ineligible_when_not_open(state):
    ok, reason = resubmit.eligibility(_pr(state=state))
    assert ok is False
    assert "not open" in reason.lower() or "unknown state" in reason.lower()


# --- operator identity: the whole point is to NOT act as the bot -----------------

def test_operator_env_drops_bot_tokens():
    swapped = resubmit.operator_env({"GH_TOKEN": "bot", "GITHUB_TOKEN": "bot",
                                     "PATH": "/usr/bin", "HOME": "/home/op"})
    assert "GH_TOKEN" not in swapped        # would authenticate gh as test-bot
    assert "GITHUB_TOKEN" not in swapped
    assert swapped["PATH"] == "/usr/bin"    # everything else is inherited
    assert swapped["HOME"] == "/home/op"    # so ssh/git credentials still resolve


# --- fork URL: ssh so the push uses the operator's key, not any GH_TOKEN ---------

def test_fork_ssh_url_from_head_repository():
    assert resubmit.fork_ssh_url(_pr()) == "git@github.com:contrib/test-repo.git"


def test_fork_ssh_url_raises_on_deleted_fork():
    with pytest.raises(RuntimeError):
        resubmit.fork_ssh_url(_pr(headRepository={}))


# --- argparse surface: push always requires an explicit content/rewrite mode -----

def test_push_requires_a_commit_message():
    with pytest.raises(SystemExit):
        resubmit.main(["42", "push"])  # -m/--message is required


def test_unknown_action_is_rejected():
    with pytest.raises(SystemExit):
        resubmit.main(["42", "frobnicate"])


def test_push_without_prepare_fails_cleanly(tmp_path, monkeypatch, capsys):
    # No prepared worktree → a clear error and a non-zero exit, no git/gh calls.
    monkeypatch.setattr(resubmit, "WORKTREE_ROOT", tmp_path / "resubmit")
    rc = resubmit.cmd_push(999, "msg", dry_run=False)
    assert rc == 2
    assert "not prepared" in capsys.readouterr().err


# --- state file placement: MUST be outside the clone, else `git add -A` at push
# time sweeps it into the contributor's PR (it once shipped a stray .resubmit.json).

def test_meta_path_is_a_sibling_of_the_clone_not_inside_it(monkeypatch, tmp_path):
    monkeypatch.setattr(resubmit, "WORKTREE_ROOT", tmp_path / "resubmit")
    wt, meta = resubmit._worktree(42), resubmit._meta_path(42)
    # The meta file cannot live under the clone dir — that's the whole bug.
    assert wt not in meta.parents
    assert meta.parent == wt.parent


def test_cleanup_removes_both_clone_and_meta(monkeypatch, tmp_path):
    monkeypatch.setattr(resubmit, "WORKTREE_ROOT", tmp_path / "resubmit")
    wt, meta = resubmit._worktree(42), resubmit._meta_path(42)
    wt.mkdir(parents=True)
    (wt / "some-file").write_text("edit")
    meta.write_text("{}")
    resubmit._cleanup(42)
    assert not wt.exists()
    assert not meta.exists()


# --- rebase: real local repos exercise the resumable Git transaction -----------

def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "Resubmit Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Resubmit Test", "GIT_COMMITTER_EMAIL": "test@example.com",
    })
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True, env=env)


def _make_rebase_repos(tmp_path: Path, *, conflict: bool = True,
                       two_conflicts: bool = False) -> dict:
    """Create an upstream + contributor fork whose fix branch needs rebasing."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "master")
    (seed / "one.txt").write_text("common one\n")
    (seed / "two.txt").write_text("common two\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "common base")

    upstream = tmp_path / "upstream.git"
    fork = tmp_path / "fork.git"
    _git(tmp_path, "clone", "--bare", str(seed), str(upstream))
    _git(tmp_path, "clone", "--bare", str(seed), str(fork))

    author = tmp_path / "author"
    _git(tmp_path, "clone", str(fork), str(author))
    _git(author, "switch", "-c", "fix")
    if conflict:
        (author / "one.txt").write_text("author one\n")
        _git(author, "add", "one.txt")
        _git(author, "commit", "-m", "change one")
        if two_conflicts:
            (author / "two.txt").write_text("author two\n")
            _git(author, "add", "two.txt")
            _git(author, "commit", "-m", "change two")
    else:
        (author / "feature.txt").write_text("feature\n")
        _git(author, "add", "feature.txt")
        _git(author, "commit", "-m", "add feature")
    _git(author, "push", "origin", "fix")
    old_head = _git(author, "rev-parse", "HEAD").stdout.strip()

    upstream_work = tmp_path / "upstream-work"
    _git(tmp_path, "clone", str(upstream), str(upstream_work))
    if conflict:
        (upstream_work / "one.txt").write_text("upstream one\n")
        if two_conflicts:
            (upstream_work / "two.txt").write_text("upstream two\n")
    else:
        (upstream_work / "base-only.txt").write_text("new base content\n")
    _git(upstream_work, "add", ".")
    _git(upstream_work, "commit", "-m", "advance base")
    _git(upstream_work, "push", "origin", "master")
    base_head = _git(upstream_work, "rev-parse", "HEAD").stdout.strip()
    # The base as it stood when the PR branched — what GitHub reports as
    # baseRefOid — distinct from base_head, the tip after master advanced.
    stale_base = _git(seed, "rev-parse", "HEAD").stdout.strip()
    return {"upstream": upstream, "fork": fork, "old_head": old_head,
            "base_head": base_head, "stale_base": stale_base}


def _wire_rebase(monkeypatch, tmp_path: Path, repos: dict) -> None:
    monkeypatch.setattr(resubmit, "WORKTREE_ROOT", tmp_path / "resubmit")
    monkeypatch.setattr(resubmit, "fork_ssh_url", lambda info: repos["fork"].as_uri())
    monkeypatch.setattr(resubmit, "upstream_ssh_url", lambda: repos["upstream"].as_uri())
    # baseRefOid is the base commit the PR was opened/last synced against, NOT
    # where the base branch points now — on any PR whose base has moved (i.e.
    # every PR a rebase is for) it names an older commit. The fixture models
    # that, so a check comparing it against the live tip fails here too.
    monkeypatch.setattr(
        resubmit, "_gh_json",
        lambda pr: _pr(headRefOid=repos["old_head"], baseRefOid=repos["stale_base"]),
    )
    monkeypatch.setattr(resubmit, "_log_rebase", lambda *args, **kwargs: None)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Resubmit Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Resubmit Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")


def test_default_prepare_keeps_the_existing_shallow_clone(monkeypatch, tmp_path):
    monkeypatch.setattr(resubmit, "WORKTREE_ROOT", tmp_path / "resubmit")
    monkeypatch.setattr(resubmit, "_gh_json", lambda pr: _pr())
    monkeypatch.setattr(resubmit, "fork_ssh_url", lambda info: "fork-url")
    calls = []

    def ran(argv, **kwargs):
        calls.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(resubmit.subprocess, "run", ran)
    assert resubmit.cmd_prepare(42) == 0
    assert calls[0][:4] == ["git", "clone", "--depth", "1"]
    assert "--filter=blob:none" not in calls[0]


def test_default_content_edit_still_pushes_fast_forward(monkeypatch, tmp_path):
    repos = _make_rebase_repos(tmp_path, conflict=False)
    _wire_rebase(monkeypatch, tmp_path, repos)
    monkeypatch.setattr(resubmit, "_log", lambda *args, **kwargs: None)
    assert resubmit.cmd_prepare(42) == 0
    wt = resubmit._worktree(42)
    (wt / "feature.txt").write_text("feature with maintainer edit\n")
    assert resubmit.cmd_push(42, "Adjust feature", False) == 0
    pushed = _git(repos["fork"], "rev-parse", "refs/heads/fix").stdout.strip()
    assert pushed != repos["old_head"]
    assert _git(repos["fork"], "rev-parse", f"{pushed}^").stdout.strip() == repos["old_head"]


def test_clean_rebase_completes_without_an_edit(monkeypatch, tmp_path, capsys):
    repos = _make_rebase_repos(tmp_path, conflict=False)
    _wire_rebase(monkeypatch, tmp_path, repos)

    assert resubmit.cmd_prepare(42, rebase=True) == 0
    meta = json.loads(resubmit._meta_path(42).read_text())
    assert meta["phase"] == "ready"
    assert meta["head_sha"] == repos["old_head"]
    assert meta["base_sha"] == repos["base_head"]
    assert meta["new_head_sha"] != repos["old_head"]
    assert "head:" in capsys.readouterr().out
    assert resubmit.cmd_diff(42) == 0
    assert repos["old_head"] in capsys.readouterr().out


def test_conflict_must_be_resolved_then_pushes_with_exact_lease(
        monkeypatch, tmp_path, capsys):
    repos = _make_rebase_repos(tmp_path)
    _wire_rebase(monkeypatch, tmp_path, repos)
    assert resubmit.cmd_prepare(42, rebase=True) == 0
    wt = resubmit._worktree(42)
    meta = json.loads(resubmit._meta_path(42).read_text())
    assert meta["phase"] == "conflicted"
    assert "one.txt" in capsys.readouterr().out

    # Git's markers cannot be staged by the helper.
    assert resubmit.cmd_continue(42) == 10
    assert "conflict markers" in capsys.readouterr().err
    (wt / "one.txt").write_text("resolved one\n")
    assert resubmit.cmd_continue(42) == 0
    meta = json.loads(resubmit._meta_path(42).read_text())
    assert meta["phase"] == "ready"
    new_head = meta["new_head_sha"]

    # The acknowledgement is exact and the remote stays untouched on a mismatch.
    assert resubmit.cmd_push(42, None, False, "deadbeef") == 11
    assert _git(repos["fork"], "rev-parse", "refs/heads/fix").stdout.strip() == repos["old_head"]

    real_git = resubmit._git
    push_calls = []

    def recording_git(args, cwd, timeout=300, extra_env=None):
        if args and args[0] == "push":
            push_calls.append(args)
        return real_git(args, cwd, timeout, extra_env)

    monkeypatch.setattr(resubmit, "_git", recording_git)
    assert resubmit.cmd_push(42, None, False, repos["old_head"]) == 0
    assert _git(repos["fork"], "rev-parse", "refs/heads/fix").stdout.strip() == new_head
    assert push_calls == [["push",
                           f"--force-with-lease=refs/heads/fix:{repos['old_head']}",
                           "origin", "HEAD:fix"]]
    assert "--force" not in push_calls[0]
    assert _git(repos["fork"], "rev-parse", "fix^").stdout.strip() == repos["base_head"]
    assert "exact force-with-lease" in capsys.readouterr().out


def test_rebase_stops_again_on_a_second_conflict(monkeypatch, tmp_path):
    repos = _make_rebase_repos(tmp_path, two_conflicts=True)
    _wire_rebase(monkeypatch, tmp_path, repos)
    assert resubmit.cmd_prepare(42, rebase=True) == 0
    wt = resubmit._worktree(42)
    assert resubmit._conflicted_paths(wt) == ["one.txt"]
    (wt / "one.txt").write_text("resolved one\n")
    assert resubmit.cmd_continue(42) == 0
    assert resubmit._conflicted_paths(wt) == ["two.txt"]
    assert json.loads(resubmit._meta_path(42).read_text())["phase"] == "conflicted"


def test_rebase_survives_a_base_that_merely_advanced(monkeypatch, tmp_path, capsys):
    # The base moving is the ordinary state of every open PR — it is why the PR
    # needs rebasing at all. A rebase pinned to a base that has since advanced is
    # simply a couple of commits behind, exactly like any other PR, so it pushes.
    repos = _make_rebase_repos(tmp_path, conflict=False)
    _wire_rebase(monkeypatch, tmp_path, repos)
    assert resubmit.cmd_prepare(42, rebase=True) == 0

    # advance upstream master after the pin is taken
    work = tmp_path / "advance"
    _git(tmp_path, "clone", str(repos["upstream"]), str(work))
    (work / "later.txt").write_text("later\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "master moves on")
    _git(work, "push", "origin", "master")

    rc = resubmit.cmd_push(42, None, dry_run=False, confirm_rewrite=repos["old_head"])
    assert rc == 0, capsys.readouterr().err
    assert _git(repos["fork"], "rev-parse", "fix").stdout.strip() != repos["old_head"]


def test_rebase_push_refuses_if_the_base_was_rewritten(monkeypatch, tmp_path, capsys):
    # A base force-pushed out from under the pin is different: the commits the
    # rebase was computed against are gone, so the pin means nothing.
    repos = _make_rebase_repos(tmp_path, conflict=False)
    _wire_rebase(monkeypatch, tmp_path, repos)
    assert resubmit.cmd_prepare(42, rebase=True) == 0

    work = tmp_path / "rewrite"
    _git(tmp_path, "clone", str(repos["upstream"]), str(work))
    _git(work, "reset", "--hard", "HEAD~1")
    (work / "divergent.txt").write_text("divergent\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "rewritten history")
    _git(work, "push", "--force", "origin", "master")

    rc = resubmit.cmd_push(42, None, dry_run=False, confirm_rewrite=repos["old_head"])
    assert rc == 6
    assert "rewritten" in capsys.readouterr().err
    assert _git(repos["fork"], "rev-parse", "fix").stdout.strip() == repos["old_head"]


def test_rebase_push_refuses_if_contributor_head_moved(monkeypatch, tmp_path, capsys):
    repos = _make_rebase_repos(tmp_path, conflict=False)
    _wire_rebase(monkeypatch, tmp_path, repos)
    assert resubmit.cmd_prepare(42, rebase=True) == 0
    monkeypatch.setattr(
        resubmit, "_gh_json",
        lambda pr: _pr(headRefOid="e" * 40, baseRefOid=repos["base_head"]),
    )
    assert resubmit.cmd_push(42, None, False, repos["old_head"]) == 6
    assert "head moved" in capsys.readouterr().err
    assert _git(repos["fork"], "rev-parse", "refs/heads/fix").stdout.strip() == repos["old_head"]


def test_abort_during_conflict_leaves_remote_untouched(monkeypatch, tmp_path):
    repos = _make_rebase_repos(tmp_path)
    _wire_rebase(monkeypatch, tmp_path, repos)
    assert resubmit.cmd_prepare(42, rebase=True) == 0
    assert resubmit.cmd_abort(42) == 0
    assert not resubmit._worktree(42).exists()
    assert not resubmit._meta_path(42).exists()
    assert _git(repos["fork"], "rev-parse", "refs/heads/fix").stdout.strip() == repos["old_head"]


# --- update: merge the base branch into the PR's head, as the operator ---------

class _Ran:
    """Records a subprocess invocation and replays a canned result."""

    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.returncode, self.stderr = returncode, stderr
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return type("R", (), {"returncode": self.returncode, "stdout": "", "stderr": self.stderr})()


def _wire_update(monkeypatch, tmp_path: Path, repos: dict) -> list[tuple]:
    """Point cmd_update at local file:// repos and capture the activity log."""
    _wire_rebase(monkeypatch, tmp_path, repos)
    logged: list[tuple] = []
    monkeypatch.setattr(resubmit, "_log_update", lambda *a, **k: logged.append((a, k)))
    return logged


def test_update_merges_the_base_branch_as_the_machine_user(monkeypatch, tmp_path, capsys):
    # A clean base merge lands a merge commit on the fork branch, authored by the
    # machine user, and leaves the contributor's own commits in place.
    repos = _make_rebase_repos(tmp_path, conflict=False)
    logged = _wire_update(monkeypatch, tmp_path, repos)
    monkeypatch.setenv("GH_TOKEN", "bot-token")

    assert resubmit.cmd_update(42, dry_run=False) == 0
    out = capsys.readouterr().out
    assert "merged master into fix as test-push-bot" in out
    assert "reingest 42" in out
    assert logged, "the update is recorded in the activity log"

    after = _git(repos["fork"], "rev-parse", "fix").stdout.strip()
    assert after != repos["old_head"], "the fork branch moved"
    parents = _git(repos["fork"], "rev-list", "--parents", "-n", "1", "fix").stdout.split()
    assert len(parents) == 3, "a merge commit with both parents"
    assert repos["old_head"] in parents and repos["base_head"] in parents
    committer = _git(repos["fork"], "log", "-1", "--format=%cn <%ce>", "fix").stdout.strip()
    assert committer == "test-push-bot <1+test-push-bot@users.noreply.github.com>"


def test_update_leaves_the_branch_untouched_on_a_conflict(monkeypatch, tmp_path, capsys):
    # A conflicting base stops at `git merge` — the contributor's branch is never
    # moved into a half-merged state.
    repos = _make_rebase_repos(tmp_path, conflict=True)
    logged = _wire_update(monkeypatch, tmp_path, repos)

    assert resubmit.cmd_update(42, dry_run=False) == 8
    err = capsys.readouterr().err
    assert "conflict" in err and "author" in err
    assert "one.txt" in err, "the conflicted path is named"
    assert not logged, "a failed merge is not logged as one that happened"
    assert _git(repos["fork"], "rev-parse", "fix").stdout.strip() == repos["old_head"]


def test_update_refuses_when_the_machine_user_cannot_push(monkeypatch, tmp_path, capsys):
    # Maintainer edits off → no push access to the fork branch, so nothing runs.
    repos = _make_rebase_repos(tmp_path, conflict=False)
    _wire_update(monkeypatch, tmp_path, repos)
    monkeypatch.setattr(resubmit, "_gh_json", lambda pr: _pr(maintainerCanModify=False))
    ran = _Ran()
    monkeypatch.setattr(resubmit.subprocess, "run", ran)

    assert resubmit.cmd_update(42, dry_run=False) == 3
    assert ran.calls == []
    assert "Allow edits from maintainers" in capsys.readouterr().err


def test_update_refuses_without_a_push_identity(monkeypatch, tmp_path, capsys):
    # An unconfigured machine returns the feature disabled, never a push under
    # whatever identity happens to be ambient.
    repos = _make_rebase_repos(tmp_path, conflict=False)
    _wire_update(monkeypatch, tmp_path, repos)
    monkeypatch.setattr(resubmit.settings, "PUSH_LOGIN", "")
    ran = _Ran()
    monkeypatch.setattr(resubmit.subprocess, "run", ran)

    assert resubmit.cmd_update(42, dry_run=False) == 3
    assert ran.calls == []
    assert "TRIAGE_PUSH_LOGIN" in capsys.readouterr().err


def test_update_dry_run_merges_nothing(monkeypatch, tmp_path, capsys):
    repos = _make_rebase_repos(tmp_path, conflict=False)
    _wire_update(monkeypatch, tmp_path, repos)
    ran = _Ran()
    monkeypatch.setattr(resubmit.subprocess, "run", ran)

    assert resubmit.cmd_update(42, dry_run=True) == 0
    assert ran.calls == [], "dry-run never clones or pushes"
    assert "[dry-run]" in capsys.readouterr().out


def test_update_needs_no_prepared_worktree(monkeypatch, tmp_path):
    # Unlike push, update owns no prepared state — it must work with nothing set up.
    repos = _make_rebase_repos(tmp_path, conflict=False)
    _wire_update(monkeypatch, tmp_path, repos)
    assert not resubmit._meta_path(42).exists()
    assert resubmit.cmd_update(42, dry_run=False) == 0
    assert not resubmit._worktree(42).exists(), "the throwaway clone is cleaned up"


def test_update_takes_no_rebase_flag():
    # The flag isn't merely unused — the CLI refuses it, so no prompt can smuggle
    # a force-push through this path.
    with pytest.raises(SystemExit):
        resubmit.main(["42", "update", "--rebase"])


def test_abort_with_only_a_stale_meta_still_cleans(monkeypatch, tmp_path):
    # A meta file with no clone (an interrupted prepare) still counts as prepared.
    monkeypatch.setattr(resubmit, "WORKTREE_ROOT", tmp_path / "resubmit")
    meta = resubmit._meta_path(42)
    meta.parent.mkdir(parents=True)
    meta.write_text("{}")
    rc = resubmit.cmd_abort(42)
    assert rc == 0
    assert not meta.exists()


# --- machine-user identity: pushes never ride the App token or an ambient key ----

def test_push_env_pins_the_key_and_identity(push_identity):
    env = resubmit.push_env({"GH_TOKEN": "app", "GITHUB_TOKEN": "app", "PATH": "/usr/bin"})
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
    # IdentitiesOnly is what stops ssh-agent offering a personal key first, which
    # would land the commit under the wrong account.
    assert "-o IdentitiesOnly=yes" in env["GIT_SSH_COMMAND"]
    assert str(push_identity) in env["GIT_SSH_COMMAND"]
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "test-push-bot"
    assert env["GIT_AUTHOR_EMAIL"].endswith("@users.noreply.github.com")
    assert env["PATH"] == "/usr/bin"


def test_push_env_refuses_without_a_configured_identity(monkeypatch):
    monkeypatch.setattr(resubmit.settings, "PUSH_LOGIN", "")
    with pytest.raises(RuntimeError, match="TRIAGE_PUSH_LOGIN"):
        resubmit.push_env()


def test_push_env_refuses_a_missing_key_file(monkeypatch, tmp_path):
    monkeypatch.setattr(resubmit.settings, "PUSH_SSH_KEY_FILE", tmp_path / "absent")
    with pytest.raises(RuntimeError, match="not a readable file"):
        resubmit.push_env()


# --- the push-target fence: a ruleset cannot scope to one account, this can ------

def test_push_target_accepts_the_prs_own_head_ref():
    resubmit.assert_push_target(_pr(), "fix")


@pytest.mark.parametrize("branch", ["master", "main", "other-pr-branch", ""])
def test_push_target_refuses_any_ref_that_is_not_the_head(branch):
    with pytest.raises(RuntimeError, match="refusing to push"):
        resubmit.assert_push_target(_pr(), branch)


def test_push_target_refuses_a_closed_pr():
    with pytest.raises(RuntimeError, match="not open"):
        resubmit.assert_push_target(_pr(state="CLOSED"), "fix")
