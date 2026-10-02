"""Threat evidence: the durable record of a PR head the threat scan flagged
malicious.

`capture` reads, from GitHub, the flagged head's diff pinned by SHA
(`compare/<base>...<head>`), the PR's diff at the head before it, and the PR,
commit, actor and force-push metadata, then appends one row to the store's
`threat_evidence` table. The head before it is the one the force-push that
produced the flagged head replaced; when GitHub shows no such force-push (it
hides a blocked actor's), it is the newest earlier head of the PR in
Prospector's shared diff cache, whose copy also stands in when GitHub no
longer serves that head's diff. Each row's `complete`
says whether the flagged diff was read whole; a read that failed is listed in
the record's `errors`. A head gets at most one incomplete row and one complete
one: a complete capture is final, and an incomplete one is written only when
the head has none, so something survives if GitHub removes the PR before a
complete read succeeds.

`export` writes a capture back out as inert text files (the diffs, the
record, a README, `SHA256SUMS`); `bundle_zip` builds the same files as a zip
in memory. Both re-hash every stored artifact first, and `export` refuses a
destination inside a git work tree.

Safety. Capture makes read-only GitHub API calls and never writes a diff to
disk. Diff bytes live only gzip-compressed in the row's binary columns; the
JSON record carries where each signature matched, never the matched text.
Nothing here hands evidence to an agent, and the store has no update or delete
for these rows.

Usage:
  uv run python -m pipeline.threat_evidence capture --pr N       # one flagged PR, now
  uv run python -m pipeline.threat_evidence capture --backfill   # every registry incident
  uv run python -m pipeline.threat_evidence list [--pr N] [--author LOGIN]
  uv run python -m pipeline.threat_evidence export --pr N [--id ID] --out DIR
  uv run python -m pipeline.threat_evidence verify [--pr N]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import sys
import zipfile
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict

from pipeline import diff_cache
from pipeline import diffpaths
from pipeline import gh
from pipeline import schema
from pipeline import settings
from pipeline import storekit
from pipeline import threats
from pipeline.store import Store

if TYPE_CHECKING:
    from pipeline.model import Pr

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


def flag_for(rec: Pr) -> Flag | None:
    """The flag on a PR record the threat scan stamped malicious: the head the
    stamp judged (a malicious verdict stays on the record whatever head INGEST
    has recorded since), its signatures and when it was stamped. None when the
    record does not read malicious."""
    threat = rec.section("threat") or {}
    head = threat.get("against_head_sha") or rec.head_sha
    if rec.threat_verdict != "malicious" or not head:
        return None
    return Flag(pr=rec.number, head_sha=head, author=rec.author,
                signatures=list(threat.get("signatures") or []),
                scanned_at=threat.get("checked_at"))


def uncaptured(store: Store, prs: dict[int, Pr]) -> list[Flag]:
    """The flags on the open PRs in `prs` whose flagged head has no complete
    capture."""
    done = store.threat_evidence_heads()
    flags = [flag_for(rec) for _, rec in sorted(prs.items()) if rec.state == "open"]
    return [f for f in flags if f is not None and (f.pr, f.head_sha) not in done]


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
        GitHub withheld nothing from it."""
        listing = diff_cache.fetch_listing(n)
        if listing is None:
            return None
        return listing.text.encode(), not listing.unread

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


@dataclass(frozen=True)
class _Prior:
    """The PR's diff at the head before the flagged one. `found_by` says how
    that head was learned: the GitHub force-push that replaced it, or the
    newest earlier head of the PR in Prospector's shared diff cache. `source`
    says where its diff came from."""
    body: bytes
    before_sha: str
    source: Literal["compare", "diff-cache"]
    found_by: Literal["force-push", "diff-cache"]
    complete: bool


def _earlier_cached_head(store: Store, flag: Flag) -> str | None:
    """The newest head of `flag`'s PR the shared diff cache fetched before the
    flagged one, or None."""
    heads = store.diff_heads(flag.pr)
    flagged_at = next((at for sha, at in heads if sha == flag.head_sha), None)
    for sha, at in heads:
        if sha != flag.head_sha and (flagged_at is None or (at or "") < flagged_at):
            return sha
    return None


def _prior(store: Store, reads: GitHubReads, flag: Flag, base: str,
           latest: ForcePush | None, errors: list[str]) -> _Prior | None:
    """The diff at the head the flagged one replaced: named by the latest
    GitHub force-push that produced the flagged head, else found in the diff
    cache; read by SHA from GitHub, else from the diff cache's copy."""
    found_by: Literal["force-push", "diff-cache"] = "force-push"
    before = latest.before if latest is not None else None
    if not before:
        found_by, before = "diff-cache", _earlier_cached_head(store, flag)
    if not before:
        return None
    body = reads.compare_diff(base, before)
    if body is not None:
        return _Prior(body[:MAX_DIFF_BYTES], before, "compare", found_by,
                      len(body) <= MAX_DIFF_BYTES)
    errors.append(f"diff at the prior head ({before[:12]}): GitHub did not answer")
    cached = store.load_diff(before)
    if cached is None:
        return None
    return _Prior(cached.encode(), before, "diff-cache", found_by, diff_cache.is_complete(cached))


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
        if listing is None:
            errors.append("per-file listing: GitHub did not answer")
        elif ((reads.pull(flag.pr) or {}).get("head") or {}).get("sha") != flag.head_sha:
            errors.append("per-file listing: the PR's head moved during the read")
        else:
            return _Diff(listing[0], "per-file-listing", listing[1])
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
            prior: _Prior | None, errors: list[str], captured_by: str | None) -> dict:
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
            "prior": None if prior is None else {
                "bytes": len(prior.body), "sha256": _sha256(prior.body),
                "source": prior.source, "found_by": prior.found_by,
                "complete": prior.complete, "before_sha": prior.before_sha},
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
    prior = _prior(store, reads, flag, base, latest, errors)

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
                   errors, reads.login())
    capture_id = store.append_threat_evidence(
        pr=flag.pr, head_sha=flag.head_sha, author=login,
        captured_at=data["provenance"]["captured_at"], complete=complete, data=data,
        diff_gz=None if diff.body is None else gzip.compress(diff.body),
        prior_gz=None if prior is None else gzip.compress(prior.body))
    status: Status = "captured" if complete else "partial"
    _ledger(store, flag, started, status, capture_id, errors)
    return status


# ---------------------------------------------------------------------------
# Export: the capture as inert text files with a hash manifest
# ---------------------------------------------------------------------------
class IntegrityError(Exception):
    """A stored artifact's bytes no longer hash to the value taken at capture."""


class ExportRefused(Exception):
    """The export destination is inside a git work tree."""


class EvidenceSummary(TypedDict):
    """A capture as the app lists it: no diff bytes, no payload text."""
    id: int
    pr: int
    head_sha: str
    author: str | None
    captured_at: str
    complete: bool
    captured_by: str | None
    machine: str | None
    artifacts: dict
    force_pushes: list[dict]
    signatures: list[str]
    errors: list[str]


def _blocks(raw: bytes) -> list[tuple[str, bytes]]:
    text = raw.decode("utf-8", "surrogateescape")
    return [(path, block.encode("utf-8", "surrogateescape"))
            for path, block in diffpaths.diff_blocks(text)]


def force_push_changes(diff: bytes, prior: bytes) -> tuple[bytes, list[str]]:
    """The file blocks of `diff` that differ from `prior`'s, each a valid patch
    against the base (what the force-push changed), and the paths `prior`
    changed that `diff` no longer does."""
    before = dict(_blocks(prior))
    after = _blocks(diff)
    changed = b"".join(block for path, block in after if before.get(path) != block)
    removed = sorted(set(before) - {path for path, _ in after})
    return changed, removed


def _artifact(blob: bytes | None, meta: dict | None, name: str) -> bytes | None:
    """The stored artifact's bytes, checked against the hash taken at capture."""
    if (blob is None) != (meta is None):
        raise IntegrityError(f"{name}: the record and the stored bytes disagree")
    if blob is None or meta is None:
        return None
    try:
        body = gzip.decompress(blob)
    except (OSError, EOFError, zlib.error) as exc:
        raise IntegrityError(f"{name}: stored bytes do not decompress ({exc})") from exc
    if _sha256(body) != meta.get("sha256"):
        raise IntegrityError(f"{name}: SHA-256 {_sha256(body)} does not match the "
                             f"{meta.get('sha256')} taken at capture")
    return body


def bundle_name(record: storekit.EvidenceRecord) -> str:
    return f"pr-{record.pr}-{record.head_sha[:7]}-evidence"


def _one_line(text: object) -> str:
    return " ".join(str(text or "").split())


def _readme(record: storekit.EvidenceRecord, names: dict[str, str],
            removed: list[str]) -> str:
    d = record.data
    prov = d.get("provenance") or {}
    actor = d.get("actor") or {}
    det = d.get("detection") or {}
    art = d.get("artifacts") or {}
    diff_meta = art.get("diff") or {}
    repo = d.get("repo") or settings.repo()
    commit = (prov.get("prospector_commit") or "")[:7] or "unknown"
    out = [
        f"# Evidence: PR #{record.pr} on {repo}",
        "",
        f"Captured {record.captured_at} by {prov.get('captured_by') or 'unknown'} on "
        f"{prov.get('machine') or 'unknown'} (Prospector {commit}), capture id {record.id}.",
        "The flagged diff was read " + (
            "whole." if record.complete else
            f"incompletely (source: {diff_meta.get('source') or 'none'}"
            f"{', truncated' if diff_meta.get('truncated') else ''}).")
        if diff_meta else "The flagged diff could not be read.",
        "",
        "**Do not** install, build, check out or run anything from this PR. Every file "
        "here is plain text. A payload can sit far to the right on a long line, so read "
        "the diffs in a viewer that wraps lines.",
        "",
        "## The PR",
        "",
        f"- URL: {d.get('url') or 'unknown'}",
        f"- Title: {_one_line(d.get('title'))}",
        f"- Author: {actor.get('login') or 'unknown'} (user id {actor.get('id')}, "
        f"account created {actor.get('created_at')})",
        f"- Fork: {(d.get('head_repo') or {}).get('full_name')} "
        f"(repo id {(d.get('head_repo') or {}).get('id')}), branch {d.get('head_ref')}",
        f"- Flagged head: {record.head_sha}",
        f"- Base: {d.get('base_sha')}",
        f"- Signatures that flagged it: {', '.join(det.get('signatures') or []) or 'none'}",
        f"- Signatures in the full diff: "
        f"{', '.join(det.get('full_diff_signatures') or []) or 'none'}",
        "",
        "## Force-pushes",
        "",
    ]
    pushes = d.get("force_pushes") or []
    out += [f"- {p.get('at')} by {p.get('actor')}: {p.get('before') or '?'} → "
            f"{p.get('after') or '?'}" for p in pushes] or ["- none recorded"]
    if removed:
        out += ["", "Files the latest force-push removed from the PR: "
                + ", ".join(removed)]
    out += ["", "## Commits", ""]
    for c in d.get("commits") or []:
        a, cm = c.get("author") or {}, c.get("committer") or {}
        out.append(f"- {c.get('sha')}: authored {a.get('date')} by {a.get('name')} "
                   f"<{a.get('email')}>; committed {cm.get('date')} by {cm.get('name')} "
                   f"<{cm.get('email')}>; signed: "
                   f"{'yes' if c.get('verified') else 'no'} ({c.get('verification_reason')})")
    if d.get("commits_truncated"):
        out.append("- (GitHub listed only the first commits)")
    matches = det.get("matches") or []
    out += ["", f"## Where the signatures matched ({names.get('diff', 'the diff')})", ""]
    out += [f"- {m.get('signature')}: {m.get('file')}, line {m.get('diff_line')}"
            for m in matches[:20]] or ["- no line matches"]
    if len(matches) > 20:
        out.append(f"- and {len(matches) - 20} more (record.json)")
    out += ["", "## Files", "", "- record.json: the capture's full record",
            "- SHA256SUMS: verify with `shasum -a 256 -c SHA256SUMS`"]
    if "diff" in names:
        out.append(f"- {names['diff']}: the PR's diff at the flagged head")
    prior_meta = art.get("prior") or {}
    if "prior" in names:
        how = ("the head the force-push replaced" if prior_meta.get("found_by") == "force-push"
               else "the newest earlier head Prospector fetched (GitHub showed no force-push "
                    "producing the flagged head)")
        out.append(f"- {names['prior']}: the PR's diff at {how}")
        out.append("- force-push-changes.diff: the files whose diff changed between that "
                   "head and the flagged one")
    shas = [record.head_sha] + ([prior_meta["before_sha"]] if prior_meta.get("before_sha")
                                else [])
    out += ["", "## Fetch the git objects", "",
            "While GitHub still serves them. A bare repository has no work tree, so "
            "nothing is checked out.", "", "```", "git init --bare evidence.git"]
    out += [f"git -C evidence.git fetch --depth=6 https://github.com/{repo}.git "
            f"{sha}:refs/evidence/{sha[:12]}" for sha in shas]
    out.append("```")
    if d.get("errors"):
        out += ["", "## Reads that failed at capture", ""]
        out += [f"- {e}" for e in d["errors"]]
    return "\n".join(out) + "\n"


def bundle_files(record: storekit.EvidenceRecord,
                 blobs: storekit.EvidenceBlobs) -> list[tuple[str, bytes]]:
    """The export's files as (name, bytes), every stored artifact re-hashed
    first: README.md, record.json, the diffs, force-push-changes.diff when the
    capture holds a prior diff, and SHA256SUMS over the rest."""
    art = record.data.get("artifacts") or {}
    diff = _artifact(blobs.diff_gz, art.get("diff"), "diff")
    prior = _artifact(blobs.prior_gz, art.get("prior"), "prior diff")
    names: dict[str, str] = {}
    files: list[tuple[str, bytes]] = []
    removed: list[str] = []
    if diff is not None:
        names["diff"] = f"pr-{record.pr}-{record.head_sha[:7]}.diff"
        files.append((names["diff"], diff))
    if prior is not None:
        before = (art.get("prior") or {}).get("before_sha") or "unknown"
        names["prior"] = f"pr-{record.pr}-{before[:7]}.prior.diff"
        files.append((names["prior"], prior))
        if diff is not None:
            changed, removed = force_push_changes(diff, prior)
            files.append(("force-push-changes.diff", changed))
    rec = {"capture_id": record.id, "complete": record.complete, **record.data}
    files = [("README.md", _readme(record, names, removed).encode()),
             ("record.json", (json.dumps(rec, indent=2, sort_keys=True) + "\n").encode()),
             *files]
    sums = "".join(f"{_sha256(body)}  {name}\n" for name, body in sorted(files))
    return [*files, ("SHA256SUMS", sums.encode())]


def bundle_zip(record: storekit.EvidenceRecord, blobs: storekit.EvidenceBlobs) -> bytes:
    """The export's files as one zip, under a `bundle_name` directory, each
    marked read-only."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in bundle_files(record, blobs):
            info = zipfile.ZipInfo(f"{bundle_name(record)}/{name}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100444 << 16
            z.writestr(info, body)
    return buf.getvalue()


def _inside_work_tree(path: Path) -> bool:
    probe = path
    while not probe.exists():
        probe = probe.parent
    res = subprocess.run(["git", "-C", str(probe), "rev-parse", "--is-inside-work-tree"],
                         capture_output=True, text=True)
    return res.returncode == 0 and res.stdout.strip() == "true"


def export(record: storekit.EvidenceRecord, blobs: storekit.EvidenceBlobs,
           out_dir: Path) -> list[Path]:
    """Write the bundle's files into `out_dir`, each read-only. Refuses a
    destination inside a git work tree, where the files could be committed, and
    an artifact whose hash moved."""
    if _inside_work_tree(out_dir):
        raise ExportRefused(f"{out_dir} is inside a git work tree; "
                            "export evidence outside any repository")
    files = bundle_files(record, blobs)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, body in files:
        path = out_dir / name
        path.write_bytes(body)
        path.chmod(0o444)
        written.append(path)
    return written


def summary(record: storekit.EvidenceRecord) -> EvidenceSummary:
    d = record.data
    prov = d.get("provenance") or {}
    return {
        "id": record.id, "pr": record.pr, "head_sha": record.head_sha,
        "author": record.author, "captured_at": record.captured_at,
        "complete": record.complete,
        "captured_by": prov.get("captured_by"), "machine": prov.get("machine"),
        "artifacts": d.get("artifacts") or {"diff": None, "prior": None},
        "force_pushes": d.get("force_pushes") or [],
        "signatures": (d.get("detection") or {}).get("signatures") or [],
        "errors": d.get("errors") or [],
    }


def log_export(store: Store, record: storekit.EvidenceRecord, *, via: str,
               operator: str | None) -> None:
    """Record one export of `record` in the runs ledger."""
    at = storekit.now()
    store.append_run({"phase": "threat-evidence:export", "started": at, "finished": at,
                      "pr": record.pr, "capture_id": record.id, "head_sha": record.head_sha,
                      "via": via, "operator": operator, "machine": settings.worker_id()})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _capture_one(store: Store, n: int) -> int:
    rec = store.load_pr(n)
    flag = None if rec is None else flag_for(rec)
    if flag is None:
        print(f"#{n} is not flagged malicious in the store; nothing to capture")
        return 1
    print(f"#{n} {flag.head_sha[:12]}: {capture(store, flag)}")
    return 0


def _backfill(store: Store) -> int:
    for inc in store.load_threats().get("incidents") or []:
        head = inc.get("head_sha")
        if not head:
            print(f"#{inc.get('pr')}: no flagged head recorded; skipped")
            continue
        flag = Flag(pr=int(inc["pr"]), head_sha=head, author=inc.get("author"),
                    signatures=list(inc.get("signatures") or []),
                    scanned_at=inc.get("noticed"))
        print(f"#{flag.pr} {head[:12]}: {capture(store, flag)}", flush=True)
    return 0


def _pick(store: Store, pr: int, capture_id: int | None) -> storekit.EvidenceRecord | None:
    if capture_id is not None:
        rec = store.threat_evidence_record(capture_id)
        return rec if rec is not None and rec.pr == pr else None
    captures = store.threat_evidence(pr=pr)
    return next((r for r in captures if r.complete), captures[0] if captures else None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Preserve, list, export and verify the "
                                             "evidence of PRs the threat scan flagged malicious.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", default=None, help="store root override (tests)")
    cap = sub.add_parser("capture", parents=[common], help="capture evidence now")
    which = cap.add_mutually_exclusive_group(required=True)
    which.add_argument("--pr", type=int, help="one PR the store has flagged malicious")
    which.add_argument("--backfill", action="store_true",
                       help="every threat-registry incident without a complete capture")
    lst = sub.add_parser("list", parents=[common], help="list captures")
    lst.add_argument("--pr", type=int)
    lst.add_argument("--author")
    exp = sub.add_parser("export", parents=[common], help="write a capture's files")
    exp.add_argument("--pr", type=int, required=True)
    exp.add_argument("--id", type=int, default=None,
                     help="capture id (default: the newest complete capture)")
    exp.add_argument("--out", required=True, help="a directory outside any git checkout")
    ver = sub.add_parser("verify", parents=[common], help="re-hash every stored artifact")
    ver.add_argument("--pr", type=int)
    args = ap.parse_args(argv)
    store = Store(args.store) if args.store else Store()

    if args.cmd == "capture":
        return _backfill(store) if args.backfill else _capture_one(store, args.pr)
    if args.cmd == "list":
        for r in store.threat_evidence(pr=args.pr, author=args.author):
            print(f"{r.id:>5}  #{r.pr}  {r.head_sha[:12]}  {r.captured_at}  "
                  f"{'complete' if r.complete else 'partial '}  {r.author or '?'}")
        return 0
    if args.cmd == "export":
        record = _pick(store, args.pr, args.id)
        blobs = None if record is None else store.threat_evidence_blobs(record.id)
        if record is None or blobs is None:
            print(f"no evidence capture for #{args.pr}")
            return 1
        try:
            paths = export(record, blobs, Path(args.out).expanduser().resolve())
        except (IntegrityError, ExportRefused) as exc:
            print(f"refused: {exc}")
            return 1
        log_export(store, record, via="cli", operator=gh.operator_login())
        for p in paths:
            print(p)
        return 0
    ok = bad = 0
    for r in store.threat_evidence(pr=args.pr):
        blobs = store.threat_evidence_blobs(r.id)
        try:
            if blobs is None:
                raise IntegrityError("row vanished")
            bundle_files(r, blobs)
            ok += 1
        except IntegrityError as exc:
            bad += 1
            print(f"capture {r.id} (#{r.pr}): {exc}")
    print(f"{ok} ok, {bad} mismatched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
