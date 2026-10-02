"""Threat evidence: the durable record of a PR head the threat scan flagged
malicious.

`capture` reads, from GitHub, the flagged head's diff pinned by SHA
(`compare/<base>...<head>`), the diff the PR had before the force-push that
produced that head, and the PR, commit, actor and force-push metadata, then
appends one row to the store's `threat_evidence` table. Each row's `complete`
says whether the flagged diff was read whole; a read that failed is listed in
the record's `errors`. A head gets at most one incomplete row and one complete
one: a complete capture is final, and an incomplete one is written only when
the head has none, so something survives if GitHub removes the PR before a
complete read succeeds.

Safety. Capture makes read-only GitHub API calls and never writes a diff to
disk. Diff bytes live only gzip-compressed in the row's binary columns; the
JSON record carries where each signature matched, never the matched text.
Nothing here hands evidence to an agent, and the store has no update or delete
for these rows.
"""
from __future__ import annotations

import gzip
import hashlib
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from pipeline import diff_cache
from pipeline import gh
from pipeline import schema
from pipeline import settings
from pipeline import storekit
from pipeline import threats

if TYPE_CHECKING:
    from pipeline.store import Store

MAX_DIFF_BYTES = 25_000_000
MAX_MATCHES = 500
DIFF_MEDIA = "application/vnd.github.diff"
REPO_ROOT = Path(__file__).resolve().parent.parent

Status = Literal["captured", "partial", "failed", "already"]
DiffSource = Literal["compare", "per-file-listing", "scan-cache"]


@dataclass(frozen=True)
class Flag:
    """A head the threat scan flagged malicious."""
    pr: int
    head_sha: str
    author: str | None
    signatures: list[str]
    scanned_at: str | None


@dataclass(frozen=True)
class ForcePush:
    """One HeadRefForcePushedEvent on the PR's timeline."""
    at: str | None
    actor: str | None
    before: str | None
    after: str | None


class GitHubReads(Protocol):
    """The GitHub reads a capture makes."""

    def pull(self, n: int) -> dict | None: ...
    def compare(self, base: str, head: str) -> dict | None: ...
    def compare_diff(self, base: str, head: str) -> bytes | None: ...
    def listing_diff(self, n: int) -> tuple[bytes, bool] | None: ...
    def force_pushes(self, n: int) -> list[ForcePush] | None: ...
    def user(self, login: str) -> dict | None: ...
    def login(self) -> str | None: ...


_FORCE_PUSHES = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: %d) {
      timelineItems(last: 100, itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT]) {
        nodes { ... on HeadRefForcePushedEvent {
          createdAt actor { login } beforeCommit { oid } afterCommit { oid } } }
      }
    }
  }
}"""


class LiveGitHub:
    """GitHubReads over the operator's `gh` login. Every call is a read."""

    def pull(self, n: int) -> dict | None:
        return gh.gh_json(f"repos/{settings.repo()}/pulls/{int(n)}")

    def compare(self, base: str, head: str) -> dict | None:
        return gh.gh_json(f"repos/{settings.repo()}/compare/{base}...{head}?per_page=100",
                          timeout=180)

    def compare_diff(self, base: str, head: str) -> bytes | None:
        return gh.gh_bytes(f"repos/{settings.repo()}/compare/{base}...{head}",
                           accept=DIFF_MEDIA, timeout=300)

    def listing_diff(self, n: int) -> tuple[bytes, bool] | None:
        """The PR's current diff rebuilt from its per-file listing, and whether
        every changed file came with its patch."""
        files = gh.pr_files(n)
        if files is None:
            return None
        parts: list[str] = []
        whole = True
        for f in files:
            name = f.get("filename", "?")
            parts.append(f"diff --git a/{name} b/{name}\n")
            if f.get("patch"):
                parts.append(f"--- a/{name}\n+++ b/{name}\n{f['patch']}\n")
            elif f.get("changes"):
                whole = False
                parts.append(f"# no patch from GitHub: {f.get('status', '?')} "
                             f"+{f.get('additions', 0)} -{f.get('deletions', 0)}\n")
        return "".join(parts).encode(), whole

    def force_pushes(self, n: int) -> list[ForcePush] | None:
        owner, _, name = settings.repo().partition("/")
        doc = gh.gh_graphql(_FORCE_PUSHES % int(n), variables={"owner": owner, "name": name})
        pull = ((((doc or {}).get("data") or {}).get("repository") or {})
                .get("pullRequest") or {})
        nodes = (pull.get("timelineItems") or {}).get("nodes")
        if not isinstance(nodes, list):
            return None
        return [ForcePush(at=nd.get("createdAt"), actor=(nd.get("actor") or {}).get("login"),
                          before=(nd.get("beforeCommit") or {}).get("oid"),
                          after=(nd.get("afterCommit") or {}).get("oid"))
                for nd in nodes if isinstance(nd, dict)]

    def user(self, login: str) -> dict | None:
        return gh.gh_json(f"users/{login}")

    def login(self) -> str | None:
        return gh.operator_login()


@dataclass(frozen=True)
class _Diff:
    body: bytes | None
    source: DiffSource | None
    whole: bool


def _flagged_diff(reads: GitHubReads, flag: Flag, base: str, head_now: str | None,
                  diffs_dir: Path, errors: list[str]) -> _Diff:
    """The flagged head's diff: the SHA-pinned compare; else, while GitHub's head
    is still the flagged one, the per-file listing; else the copy the scan read."""
    body = reads.compare_diff(base, flag.head_sha)
    if body is not None:
        return _Diff(body, "compare", True)
    errors.append(f"diff {base[:12]}...{flag.head_sha[:12]}: GitHub did not answer")
    if head_now == flag.head_sha:
        listing = reads.listing_diff(flag.pr)
        if listing is not None:
            return _Diff(listing[0], "per-file-listing", listing[1])
        errors.append("per-file listing: GitHub did not answer")
    cached = diffs_dir / f"{flag.head_sha}.diff"
    if cached.exists():
        return _Diff(cached.read_bytes(), "scan-cache", False)
    return _Diff(None, None, False)


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _commit() -> str | None:
    """This checkout's commit, or None outside a git checkout."""
    try:
        res = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def _commits(cmp: dict | None) -> tuple[list[dict], bool]:
    """The compare's commits as {sha, author, committer, verified,
    verification_reason}, and whether GitHub listed fewer than the total."""
    raw = (cmp or {}).get("commits") or []
    out: list[dict] = []
    for c in raw:
        commit = c.get("commit") or {}
        verification = commit.get("verification") or {}
        out.append({
            "sha": c.get("sha"),
            "author": {k: (commit.get("author") or {}).get(k) for k in ("name", "email", "date")},
            "committer": {k: (commit.get("committer") or {}).get(k)
                          for k in ("name", "email", "date")},
            "verified": verification.get("verified"),
            "verification_reason": verification.get("reason"),
        })
    total = (cmp or {}).get("total_commits")
    return out, isinstance(total, int) and total > len(out)


def _record(flag: Flag, pull: dict | None, cmp: dict | None, actor: dict | None,
            login: str | None, pushes: list[ForcePush], diff: _Diff, truncated: bool,
            prior: bytes | None, latest: ForcePush | None, errors: list[str],
            captured_by: str | None) -> dict:
    """The capture's `data` record. It names where signatures matched in the
    stored diff and carries no diff text."""
    pull = pull or {}
    head = pull.get("head") or {}
    text = diff.body.decode("utf-8", "replace") if diff.body is not None else ""
    commits, commits_truncated = _commits(cmp)
    located = threats.locate(text, limit=MAX_MATCHES)
    full: set[str] = set()
    if diff.body is not None:
        full = set(threats.scan_diff(text)["signatures"]) | {m.signature for m in located}
    return {
        "pr": flag.pr,
        "url": pull.get("html_url"),
        "repo": settings.repo(),
        "title": pull.get("title"),
        "body": pull.get("body"),
        "state": pull.get("state"),
        "created_at": pull.get("created_at"),
        "base_sha": (pull.get("base") or {}).get("sha"),
        "base_ref": (pull.get("base") or {}).get("ref"),
        "head_sha": flag.head_sha,
        "head_ref": head.get("ref"),
        "head_repo": {"full_name": (head.get("repo") or {}).get("full_name"),
                      "id": (head.get("repo") or {}).get("id")},
        "actor": {"login": login, "id": (actor or {}).get("id"),
                  "type": (actor or {}).get("type"),
                  "created_at": (actor or {}).get("created_at")},
        "commits": commits,
        "commits_truncated": commits_truncated,
        "force_pushes": [asdict(p) for p in pushes],
        "detection": {
            "verdict": "malicious",
            "signatures": list(flag.signatures),
            "scanned_at": flag.scanned_at,
            "matches": [asdict(m) for m in located],
            "full_diff_signatures": sorted(full),
        },
        "artifacts": {
            "diff": None if diff.body is None else {
                "bytes": len(diff.body), "sha256": _sha256(diff.body), "source": diff.source,
                "complete": diff.whole and not truncated, "truncated": truncated},
            "prior": None if prior is None or latest is None else {
                "bytes": len(prior), "sha256": _sha256(prior), "source": "compare",
                "before_sha": latest.before},
        },
        "provenance": {
            "captured_at": storekit.now(),
            "captured_by": captured_by,
            "machine": settings.worker_id(),
            "prospector_commit": _commit(),
            "store_schema": schema.STORE_SCHEMA_VERSION,
        },
        "errors": errors,
    }


def _ledger(store: Store, flag: Flag, started: str, status: Status, capture_id: int,
            errors: list[str]) -> None:
    store.append_run({"phase": "threat-evidence:capture", "started": started,
                      "finished": storekit.now(), "pr": flag.pr, "head_sha": flag.head_sha,
                      "id": capture_id, "status": status, "errors": errors})


def capture(store: Store, flag: Flag, *, github: GitHubReads | None = None,
            diffs_dir: Path | None = None) -> Status:
    """Capture the evidence for `flag`'s head and append it as one row, unless
    the head already has a complete capture (`already`). A GitHub failure never
    raises: what could not be read is listed in the record's `errors`, and a
    capture that read nothing at all, or that would be the head's second
    incomplete row, writes nothing (`failed`)."""
    existing = [r for r in store.threat_evidence(pr=flag.pr) if r.head_sha == flag.head_sha]
    if any(r.complete for r in existing):
        return "already"
    reads = github or LiveGitHub()
    errors: list[str] = []
    started = storekit.now()

    pull = reads.pull(flag.pr)
    if pull is None:
        errors.append("pull request metadata: GitHub did not answer")
    base = ((pull or {}).get("base") or {}).get("sha") or settings.default_branch()
    head_now = ((pull or {}).get("head") or {}).get("sha")

    diff = _flagged_diff(reads, flag, base, head_now, diffs_dir or diff_cache.DIFFS, errors)
    truncated = diff.body is not None and len(diff.body) > MAX_DIFF_BYTES
    if diff.body is not None and truncated:
        diff = _Diff(diff.body[:MAX_DIFF_BYTES], diff.source, diff.whole)
    complete = diff.body is not None and diff.whole and not truncated

    pushes = reads.force_pushes(flag.pr)
    if pushes is None:
        errors.append("force-push history: GitHub did not answer")
    latest = next((p for p in reversed(pushes or []) if p.after == flag.head_sha), None)
    prior: bytes | None = None
    if latest is not None and latest.before:
        prior = reads.compare_diff(base, latest.before)
        if prior is None:
            errors.append(f"diff before the force-push ({latest.before[:12]}): "
                          "GitHub did not answer")
        elif len(prior) > MAX_DIFF_BYTES:
            prior = prior[:MAX_DIFF_BYTES]

    cmp = reads.compare(base, flag.head_sha)
    if cmp is None:
        errors.append("commits: GitHub did not answer")
    login = flag.author or ((pull or {}).get("user") or {}).get("login")
    actor = reads.user(login) if login else None
    if login and actor is None:
        errors.append(f"actor {login}: GitHub did not answer")

    if pull is None and diff.body is None and cmp is None:
        return "failed"
    if not complete and existing:
        return "failed"

    data = _record(flag, pull, cmp, actor, login, pushes or [], diff, truncated, prior,
                   latest, errors, reads.login())
    capture_id = store.append_threat_evidence(
        pr=flag.pr, head_sha=flag.head_sha, author=login,
        captured_at=data["provenance"]["captured_at"], complete=complete, data=data,
        diff_gz=None if diff.body is None else gzip.compress(diff.body),
        prior_gz=None if prior is None else gzip.compress(prior))
    status: Status = "captured" if complete else "partial"
    _ledger(store, flag, started, status, capture_id, errors)
    return status
