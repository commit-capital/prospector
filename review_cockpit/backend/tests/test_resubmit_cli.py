"""The agent's `resubmit` helper — author a change on a contributor's fork branch
and push it AS THE OPERATOR (#210). The git/gh mechanics need a live fork, so the
unit tests here lock down the safety-critical PURE logic: the push-eligibility
preflight, the operator-identity env swap (drop the bot token), the fork ssh URL,
and the argparse surface. The module has no `.py` extension (it runs under the
sandbox's bare python3), so we load it by path."""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RESUBMIT_PATH = REPO_ROOT / "review_cockpit" / "agent" / "resubmit"


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


# --- eligibility: the preflight that decides whether a push is even possible ----

def _pr(**over):
    base = {"number": 42, "state": "OPEN", "baseRefName": "master", "headRefName": "fix",
            "headRefOid": "a" * 40, "isCrossRepository": True,
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


# --- argparse surface: the three subcommands, and push requires a message --------

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


# --- update: merge the base branch into the PR's head, as the operator ---------

class _Ran:
    """Records the `gh pr update-branch` invocation and replays a canned result."""

    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.returncode, self.stderr = returncode, stderr
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return type("R", (), {"returncode": self.returncode, "stdout": "", "stderr": self.stderr})()


@pytest.fixture
def stub_update(monkeypatch):
    """Stub the three live edges of cmd_update: the PR read, the poll's sleep, and
    the activity log. The PR read models GitHub's async update — the head still reads
    as the old sha on the first poll after the merge is accepted, and moves on the
    next one."""
    reads = iter([_pr(), _pr(), _pr(headRefOid="b" * 40)])
    monkeypatch.setattr(resubmit, "_gh_json",
                        lambda pr: next(reads, _pr(headRefOid="b" * 40)))
    monkeypatch.setattr(resubmit.time, "sleep", lambda s: None)
    logged: list[tuple] = []
    monkeypatch.setattr(resubmit, "_log_update",
                        lambda *a, **k: logged.append((a, k)))
    return logged


def test_update_merges_the_base_branch_as_the_operator(monkeypatch, stub_update, capsys):
    # The merge runs `gh pr update-branch` with the bot token DROPPED — an App token
    # can't write the `.github/workflows/**` a moved base branch carries in.
    monkeypatch.setenv("GH_TOKEN", "bot-token")
    ran = _Ran()
    monkeypatch.setattr(resubmit.subprocess, "run", ran)
    rc = resubmit.cmd_update(42, dry_run=False)
    assert rc == 0
    argv, kw = ran.calls[0]
    assert argv[:3] == ["gh", "pr", "update-branch"]
    assert "GH_TOKEN" not in kw["env"]
    # --rebase force-pushes over the contributor's commits; it is never passed.
    assert "--rebase" not in argv
    assert stub_update, "the update is recorded in the activity log"
    out = capsys.readouterr().out
    assert "master" in out and "reingest 42" in out
    # The new head is reported, not the pre-merge sha the first poll still returns.
    assert "aaaaaaaa → bbbbbbbb" in out


def test_update_reports_honestly_when_the_new_head_never_appears(monkeypatch, capsys):
    # GitHub accepts the merge asynchronously; if the head hasn't moved by the end of
    # the poll we must NOT claim a sha transition that we never observed.
    monkeypatch.setattr(resubmit, "_gh_json", lambda pr: _pr())
    monkeypatch.setattr(resubmit.time, "sleep", lambda s: None)
    logged: list[tuple] = []
    monkeypatch.setattr(resubmit, "_log_update", lambda *a, **k: logged.append(a))
    monkeypatch.setattr(resubmit.subprocess, "run", _Ran())
    assert resubmit.cmd_update(42, dry_run=False) == 0
    out = capsys.readouterr().out
    assert "Confirm it landed" in out
    assert "→" not in out, "no sha transition is claimed"
    assert logged[0][3] is None, "the unobserved head is logged as None, not the old sha"


def test_update_refuses_when_the_operator_cannot_push(monkeypatch, stub_update, capsys):
    # Maintainer edits off → no push access to the fork branch, so no gh call at all.
    monkeypatch.setattr(resubmit, "_gh_json", lambda pr: _pr(maintainerCanModify=False))
    ran = _Ran()
    monkeypatch.setattr(resubmit.subprocess, "run", ran)
    assert resubmit.cmd_update(42, dry_run=False) == 3
    assert ran.calls == []
    assert "Allow edits from maintainers" in capsys.readouterr().err


def test_update_reports_a_conflict_as_the_authors_to_resolve(monkeypatch, stub_update, capsys):
    ran = _Ran(returncode=1, stderr="GraphQL: merge conflict between base and head")
    monkeypatch.setattr(resubmit.subprocess, "run", ran)
    assert resubmit.cmd_update(42, dry_run=False) == 8
    err = capsys.readouterr().err
    assert "conflict" in err and "author" in err
    assert not stub_update, "a failed merge is not logged as one that happened"


def test_update_dry_run_merges_nothing(monkeypatch, stub_update, capsys):
    ran = _Ran()
    monkeypatch.setattr(resubmit.subprocess, "run", ran)
    assert resubmit.cmd_update(42, dry_run=True) == 0
    assert ran.calls == [], "dry-run never shells out to gh"
    assert "[dry-run]" in capsys.readouterr().out


def test_update_needs_no_prepared_worktree(monkeypatch, tmp_path, stub_update):
    # Unlike push, update owns no local clone — it must work with nothing prepared.
    monkeypatch.setattr(resubmit, "WORKTREE_ROOT", tmp_path / "resubmit")
    monkeypatch.setattr(resubmit.subprocess, "run", _Ran())
    assert resubmit.cmd_update(42, dry_run=False) == 0


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
