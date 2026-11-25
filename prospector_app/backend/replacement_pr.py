"""Open a pull request that replaces one whose fork branch cannot be pushed to.

`resubmit push` calls this when the confirming operator has no write access to
the contributor's fork. The finished head of the prepared clone, which carries
the contributor's own commits, goes to
`prospector/replace-pr-<n>-<original head sha[:8]>` on the push user's fork
(`propose.assert_push_fork`), and the bot opens the pull request from it
(`safety_guard.replace_bot_run`). Nothing in the branch name comes from
contributor text, because a `pull_request_target` workflow upstream receives
it. A branch is never overwritten: one that already holds this same head is
reused, so a pull request whose opening failed after its push can be retried.
The original pull request is not touched.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from issue_triage import propose
from pipeline import bot_token, gh, settings
from prospector_app.backend import resubmit_identity, safety_guard

# GitHub caps a pull request body at 65536 characters.
BODY_CHARS = 60000


def branch_ref(pr: int, head_sha: str) -> str:
    return f"prospector/replace-pr-{pr}-{head_sha[:8]}"


def available(info: dict) -> tuple[bool, str]:
    """Whether pull request `info` can be replaced from this machine, and why
    not."""
    if not settings.push_identity_configured():
        return False, "this machine has no contributor-push identity"
    if not bot_token.available():
        return False, "this machine cannot mint a bot token"
    if info.get("baseRefName") != settings.default_branch():
        return False, f"it does not target {settings.default_branch()}"
    if (info.get("headRepositoryOwner") or {}).get("login") == settings.push_login():
        return False, "its branch is already on the push user's fork"
    return True, ""


def body(info: dict) -> str:
    author = (info.get("author") or {}).get("login")
    by = f" by @{author}" if author else ""
    return (f"Replaces #{info['number']}{by}. Only its author can push to that branch, so "
            "this pull request carries the same commits with the remaining work on top.\n\n"
            f"---\n\n{info.get('body') or ''}")[:BODY_CHARS]


def _git(wt: Path, *args: str, env: dict[str, str]) -> str:
    done = subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True,
                          env=env, timeout=600)
    if done.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {(done.stderr or done.stdout).strip()[-400:]}")
    return done.stdout


def open_pr(info: dict, wt: Path, sha: str) -> dict:
    """Push commit `sha` of clone `wt` to the replacement branch and open the
    pull request, returning its {number, url, ref}. An open pull request the
    branch already has is returned as it is. Raises RuntimeError at the first
    step that fails."""
    ref = branch_ref(int(info["number"]), info["headRefOid"])
    head = f"{settings.push_login()}:{ref}"
    env = resubmit_identity.push_env()
    url = propose.fork_url()
    propose.assert_push_fork(propose.fork_state(), url)
    if _git(wt, "rev-parse", "--is-shallow-repository", env=env).strip() == "true":
        _git(wt, "fetch", "--quiet", "--unshallow", "origin",
             env=resubmit_identity.operator_env())
    on_fork = _git(wt, "ls-remote", url, f"refs/heads/{ref}", env=env).split()
    if on_fork and on_fork[0] != sha:
        raise RuntimeError(f"{ref} already exists on the fork with other content")
    if on_fork:
        pulls = gh.gh_list(f"repos/{settings.repo()}/pulls?head={head}&state=all")
        if pulls is None:
            raise RuntimeError(f"the pull requests from {ref} cannot be read")
        for pull in pulls:
            if pull.get("state") != "open":
                raise RuntimeError(f"#{pull['number']} from {ref} was already closed")
            return {"number": int(pull["number"]), "url": pull.get("html_url"), "ref": ref}
    else:
        _git(wt, "push", "--quiet", f"--force-with-lease=refs/heads/{ref}:", url,
             f"{sha}:refs/heads/{ref}", env=env)
    r = safety_guard.replace_bot_run({
        "title": str(info.get("title") or f"Replace #{info['number']}"), "body": body(info),
        "head": head, "base": settings.default_branch(), "maintainer_can_modify": False})
    if r.returncode != 0:
        raise RuntimeError(f"pushed {ref}, but opening the pull request failed: "
                           f"{(r.stderr or r.stdout).strip()[-300:]}")
    pull = json.loads(r.stdout)
    return {"number": int(pull["number"]), "url": pull.get("html_url"), "ref": ref}


def ingest(number: int) -> str | None:
    """Store pull request `number`, returning why that failed or None."""
    try:
        from pipeline import ingest as pr_ingest
        from pipeline.store import Store
        pr_ingest.refresh_prs(Store(), [number])
    except Exception as e:  # noqa: BLE001 — the pull request is open either way
        return f"{type(e).__name__}: {e}"
    return None
