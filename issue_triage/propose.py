"""Push an issue-fix lane's proven patch to the contributor-push user's fork, as
the branch a proposed pull request opens from.

The push user (`settings.push_login()`, SSH key only — `resubmit_identity.
push_env`) pushes to its own fork of `TRIAGE_REPO`; it holds no API token, and
the App that opens the pull request cannot push. `push_fix` clones the
upstream repository, checks out the base the lane proved the fix on, applies
the lane's exact patch, and commits it as one commit on that base. Before any
push, `assert_propose_target` holds the destination to the fence:

- the fork, read live, is a fork of `TRIAGE_REPO` owned by the push login and
  not archived, and `origin` points at it and nowhere else;
- the branch is `prospector/issue-<n>-<report sha[:8]>` for the report's
  first proposal, and that name with `-<k>` (2 to MAX_ATTEMPTS) for its k-th —
  nothing in the name comes from issue text, because a `pull_request_target`
  workflow upstream receives it;
- HEAD has exactly one parent, the proven base, which the freshly fetched
  upstream default branch contains;
- the branch does not exist on the fork yet, or already holds this same
  change on this same base (a proposal whose opening failed after its push is
  reused); a proposal never overwrites a branch;
- no earlier branch of the same report, whose pull request was closed, holds
  this same code.

`push_revision` puts a revised fix on a lane branch whose pull request is
open: one commit on the branch's current head, leased on it, under the same
fence. The fork is never created here; the push user's account holds it. The command
`python -m issue_triage.propose --issue N [--live]` proposes the issue's last
lane result through `executor.propose_issue_fix`.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pipeline import gh, settings

REF_RE = re.compile(r"^prospector/issue-([1-9][0-9]{0,8})-([0-9a-f]{8})(?:-([2-9]))?$")
# The most proposals one report's lane branches carry.
MAX_ATTEMPTS = 9
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ProposeRefused(RuntimeError):
    """The push does not satisfy the fence, or its preconditions do not hold."""


@dataclass(frozen=True)
class Pushed:
    ref: str
    head_sha: str
    tree_sha: str
    pushed: bool
    reused: bool = False


def branch_ref(issue: int, report_sha: str, attempt: int = 1) -> str:
    """The lane branch of the report's `attempt`-th proposal."""
    if not 1 <= attempt <= MAX_ATTEMPTS:
        raise ValueError(f"attempt {attempt} is outside 1..{MAX_ATTEMPTS}")
    base = f"prospector/issue-{issue}-{report_sha[:8]}"
    return base if attempt == 1 else f"{base}-{attempt}"


def attempt_of(ref: str, issue: int, report_sha: str) -> int | None:
    """Which proposal of issue `issue`'s report `ref` is the lane branch of, or
    None when it is not one of them."""
    m = REF_RE.fullmatch(ref)
    if not m or int(m.group(1)) != issue or m.group(2) != report_sha[:8]:
        return None
    return int(m.group(3) or 1)


def upstream_url() -> str:
    return f"https://github.com/{settings.repo()}.git"


def fork_url() -> str:
    return f"git@github.com:{settings.push_login()}/{settings.repo().split('/', 1)[1]}.git"


def fork_state() -> dict | None:
    """The push user's fork of TRIAGE_REPO as GitHub reports it, read as the
    operator, or None when it cannot be read."""
    return gh.gh_json(f"repos/{settings.push_login()}/{settings.repo().split('/', 1)[1]}")


def assert_propose_target(fork: dict | None, origin_url: str, ref: str, issue: int,
                          report8: str) -> None:
    """Raise ProposeRefused unless the push destination is the push user's own
    fork of TRIAGE_REPO and `ref` is this issue's lane branch."""
    if not settings.push_login():
        raise ProposeRefused("no contributor-push login is configured")
    if origin_url != fork_url() or origin_url == upstream_url():
        raise ProposeRefused(f"origin is {origin_url!r}, not the push user's fork")
    if not fork:
        raise ProposeRefused(f"the fork {settings.push_login()}/"
                             f"{settings.repo().split('/', 1)[1]} cannot be read — create it "
                             "as the push user first")
    parent = (fork.get("parent") or {}).get("full_name")
    owner = (fork.get("owner") or {}).get("login")
    if not fork.get("fork") or parent != settings.repo():
        raise ProposeRefused(f"{fork.get('full_name')} is not a fork of {settings.repo()}")
    if owner != settings.push_login():
        raise ProposeRefused(f"the fork is owned by {owner!r}, not {settings.push_login()!r}")
    if fork.get("archived") or fork.get("private"):
        raise ProposeRefused("the fork is archived or private")
    if attempt_of(ref, issue, report8) is None:
        raise ProposeRefused(f"{ref!r} is not issue #{issue}'s lane branch")


def _git(repo: Path, *args: str, env: dict[str, str] | None = None,
         input: str | None = None) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=env, input=input, timeout=600)
    if done.returncode != 0:
        raise ProposeRefused(f"git {args[0]} failed: {(done.stderr or done.stdout).strip()[-400:]}")
    return done.stdout


def push_fix(*, issue: int, report_sha: str, base_sha: str, patch: str, message: str,
             workdir: Path, dry_run: bool, attempt: int = 1) -> Pushed:
    """Commit `patch` on `base_sha` and push it to the fork's lane branch for
    the report's `attempt`-th proposal — everything but the push when
    `dry_run`. An earlier attempt's branch holding this same code refuses the
    push. Raises ProposeRefused at the first precondition or fence rule that
    fails."""
    from prospector_app.backend import resubmit_identity

    if not _SHA_RE.fullmatch(base_sha):
        raise ProposeRefused(f"{base_sha!r} is not a full commit sha")
    ref = branch_ref(issue, report_sha, attempt)
    env = resubmit_identity.push_env()
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(["git", "clone", "--quiet", "--no-checkout", upstream_url(),
                           str(workdir)],
                          capture_output=True, text=True, env=env, timeout=900)
    if done.returncode != 0:
        raise ProposeRefused(f"cloning {settings.repo()} failed: {done.stderr.strip()[-400:]}")
    try:
        _git(workdir, "remote", "rename", "origin", "upstream")
        _git(workdir, "remote", "set-url", "--push", "upstream", "DISABLED")
        _git(workdir, "remote", "add", "origin", fork_url())
        default = settings.default_branch()
        _git(workdir, "fetch", "--quiet", "upstream", default, env=env)
        try:
            _git(workdir, "merge-base", "--is-ancestor", base_sha, "FETCH_HEAD")
        except ProposeRefused as e:
            raise ProposeRefused(f"the proven base {base_sha[:12]} is not on "
                                 f"{settings.repo()}'s {default}") from e
        _git(workdir, "checkout", "--quiet", "--detach", base_sha)
        try:
            _git(workdir, "apply", "--index", "--whitespace=nowarn", "-", input=patch)
        except ProposeRefused as e:
            raise ProposeRefused(f"the patch no longer applies to {base_sha[:12]}: {e}") from e
        _git(workdir, "commit", "--quiet", "--no-gpg-sign", "-m", message, env=env)
        head = _git(workdir, "rev-parse", "HEAD").strip()
        parents = _git(workdir, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
        if parents != [base_sha]:
            raise ProposeRefused(f"the commit's parents are {parents}, not [{base_sha[:12]}]")
        tree = _git(workdir, "rev-parse", "HEAD^{tree}").strip()
        origin_url = _git(workdir, "config", "--get", "remote.origin.url").strip()
        assert_propose_target(fork_state(), origin_url, ref, issue, report_sha[:8])
        for earlier in (branch_ref(issue, report_sha, k) for k in range(1, attempt)):
            if not _git(workdir, "ls-remote", "--heads", "origin", earlier, env=env).strip():
                continue
            _git(workdir, "fetch", "--quiet", "origin", f"refs/heads/{earlier}", env=env)
            if _git(workdir, "rev-parse", "FETCH_HEAD^{tree}").strip() == tree:
                raise ProposeRefused(f"{earlier} already holds this same code, and its pull "
                                     "request was closed without merging")
        if _git(workdir, "ls-remote", "--heads", "origin", ref, env=env).strip():
            _git(workdir, "fetch", "--quiet", "origin", f"refs/heads/{ref}", env=env)
            existing = _git(workdir, "rev-parse", "FETCH_HEAD").strip()
            same = (_git(workdir, "rev-parse", "FETCH_HEAD^{tree}").strip() == tree
                    and _git(workdir, "rev-parse", "FETCH_HEAD^").strip() == base_sha)
            if not same:
                raise ProposeRefused(f"{ref} already exists on the fork with other content")
            return Pushed(ref=ref, head_sha=existing, tree_sha=tree, pushed=False, reused=True)
        if not dry_run:
            _git(workdir, "push", "--quiet", f"--force-with-lease=refs/heads/{ref}:", "origin",
                 f"HEAD:refs/heads/{ref}", env=env)
        return Pushed(ref=ref, head_sha=head, tree_sha=tree, pushed=not dry_run)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def push_revision(*, issue: int, report_sha: str, base_sha: str, patch: str, message: str,
                  expected_head: str, workdir: Path, dry_run: bool, attempt: int = 1) -> Pushed:
    """Push a revised `patch`, proven on `base_sha`, to the lane branch of the
    report's `attempt`-th proposal as one commit on top of `expected_head` —
    the head its open pull request shows. The commit's tree is exactly
    `base_sha` plus `patch`; its parents are
    `expected_head`, and `base_sha` too when the branch does not contain it (a
    revision proven on a newer base). The push is a fast-forward leased on
    `expected_head`, so a branch that moved is refused, never overwritten. An
    unchanged tree pushes nothing. Everything but the push when `dry_run`."""
    from prospector_app.backend import resubmit_identity

    for name, sha in (("base", base_sha), ("expected head", expected_head)):
        if not _SHA_RE.fullmatch(sha):
            raise ProposeRefused(f"the {name} {sha!r} is not a full commit sha")
    ref = branch_ref(issue, report_sha, attempt)
    env = resubmit_identity.push_env()
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(["git", "clone", "--quiet", "--no-checkout", upstream_url(),
                           str(workdir)],
                          capture_output=True, text=True, env=env, timeout=900)
    if done.returncode != 0:
        raise ProposeRefused(f"cloning {settings.repo()} failed: {done.stderr.strip()[-400:]}")
    try:
        _git(workdir, "remote", "rename", "origin", "upstream")
        _git(workdir, "remote", "set-url", "--push", "upstream", "DISABLED")
        _git(workdir, "remote", "add", "origin", fork_url())
        default = settings.default_branch()
        _git(workdir, "fetch", "--quiet", "upstream", default, env=env)
        try:
            _git(workdir, "merge-base", "--is-ancestor", base_sha, "FETCH_HEAD")
        except ProposeRefused as e:
            raise ProposeRefused(f"the proven base {base_sha[:12]} is not on "
                                 f"{settings.repo()}'s {default}") from e
        origin_url = _git(workdir, "config", "--get", "remote.origin.url").strip()
        assert_propose_target(fork_state(), origin_url, ref, issue, report_sha[:8])
        _git(workdir, "fetch", "--quiet", "origin", f"refs/heads/{ref}", env=env)
        current = _git(workdir, "rev-parse", "FETCH_HEAD").strip()
        if current != expected_head:
            raise ProposeRefused(f"{ref} is at {current[:12]}, not {expected_head[:12]}; "
                                 "someone else pushed to it")
        _git(workdir, "checkout", "--quiet", "--detach", base_sha)
        try:
            _git(workdir, "apply", "--index", "--whitespace=nowarn", "-", input=patch)
        except ProposeRefused as e:
            raise ProposeRefused(f"the patch does not apply to {base_sha[:12]}: {e}") from e
        tree = _git(workdir, "write-tree").strip()
        if tree == _git(workdir, "rev-parse", f"{expected_head}^{{tree}}").strip():
            return Pushed(ref=ref, head_sha=expected_head, tree_sha=tree, pushed=False,
                          reused=True)
        parents = ["-p", expected_head]
        try:
            _git(workdir, "merge-base", "--is-ancestor", base_sha, expected_head)
        except ProposeRefused:
            parents += ["-p", base_sha]
        head = _git(workdir, "commit-tree", tree, *parents, "-m", message, env=env).strip()
        if not dry_run:
            _git(workdir, "push", "--quiet",
                 f"--force-with-lease=refs/heads/{ref}:{expected_head}", "origin",
                 f"{head}:refs/heads/{ref}", env=env)
        return Pushed(ref=ref, head_sha=head, tree_sha=tree, pushed=not dry_run)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def result_dir(issue: int) -> Path:
    """Where `fix_lane` keeps the issue's latest run."""
    return settings.verify_scratch() / "issue-fix" / f"issue-{issue}"


def load_result(issue: int) -> dict | None:
    """The issue's latest lane result, as `fix_lane` wrote it, or None."""
    path = result_dir(issue) / "result.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m issue_triage.propose",
        description="Open a pull request for an issue's last fix-lane result, for a "
                    "maintainer to review. A dry-run unless --live.")
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--live", action="store_true",
                    help="push the branch and open the pull request as the bot")
    args = ap.parse_args(argv)
    from prospector_app.backend import executor

    token = executor.mint_bot_token() if args.live else None
    res = executor.propose_issue_fix(args.issue, token=token, dry_run=not args.live)
    print(json.dumps(res, indent=2))
    return 0 if res["status"] in ("executed", "dry-run", "exists") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
