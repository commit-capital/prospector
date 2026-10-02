"""Machine-local PR diff cache and the bounded, read-only GitHub fetch into it.

One file per PR head (`cache/diffs/<head_sha>.diff`), bounded by MAX_DIFF_BYTES:
`bound` spends the cap on source files first and replaces every file past it
with a one-line stub, so a cached diff always names every changed file.
Fetches are read-only against GitHub: `gh pr diff`, falling back to a diff
synthesized from the paginated per-file listing when GitHub refuses the diff
outright (HTTP 406 for PRs over 20k lines). The CLUSTER wave's fetch-diffs
stage and the threat scan's fetch-missing step both fetch through here.

With a `store` supplied, fetches read through the shared `diffs` table: local
file, then the store, then GitHub — writing back at each level, so one
machine's fetch spares every other machine the download. A head's diff never
changes, so a store row is fresh by construction. Store failures degrade to
the direct GitHub fetch (it is a cache, not a gate), logged as warnings.

A reader that must see every added line — the threat scan — checks
`is_complete` and, for a copy that is not, reads the whole diff with
`fetch_complete`, which never touches the cache or the store and names the
files GitHub itself withheld.

Every function that touches the cache takes an optional `diffs_dir` override
(tests, alternate caches); None means the canonical DIFFS directory.
"""
from __future__ import annotations

import logging
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import settings
from pipeline import diffpaths
from pipeline import gh
from pipeline import profile
from pipeline import progress
from pipeline.gh import operator_env

if TYPE_CHECKING:
    from pipeline.store import Store
    from pipeline.wire import DiffManifestItem

_log = logging.getLogger(__name__)

DIFFS = Path(__file__).resolve().parent / "cache" / "diffs"
MAX_DIFF_BYTES = 200_000  # summarizers don't need megadiffs


def _artifact_category(path: str) -> str | None:
    for rule in profile.active().artifact_rules:
        if re.search(rule.pattern, path, re.IGNORECASE):
            return rule.category
    return None


def _stub(block: str, category: str | None) -> str:
    """A file's `diff --git` header plus one line naming what the cache left
    out: its artifact category (or `source`), byte size, and +/- line counts."""
    header, _, body = block.partition("\n")
    adds = dels = 0
    for line in body.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            adds += 1
        elif line.startswith("-") and not line.startswith("---"):
            dels += 1
    return f"{header}\n# omitted: {category or 'source'}, {len(block)} bytes, +{adds} -{dels}\n"


def bound(text: str, cap: int | None = None) -> str:
    """The diff the cache holds for `text`: every non-artifact file in its
    original order, then every artifact-category file (the profile's
    `artifact_rules`), each kept whole while the running size stays within
    `cap` and stubbed from the first file that does not fit onward. Every
    changed file keeps its header, so `changed_paths` over the result is
    complete however large the PR."""
    limit = MAX_DIFF_BYTES if cap is None else cap
    m = re.search(r"^diff --git ", text or "", re.M)
    preamble = text[:m.start()] if m else (text or "")
    blocks = [(path, block, _artifact_category(path))
              for path, block in diffpaths.diff_blocks(text)]
    ordered = [b for b in blocks if b[2] is None] + [b for b in blocks if b[2] is not None]
    out = [preamble]
    used = len(preamble)
    exceeded = False
    for _, block, category in ordered:
        if not exceeded and used + len(block) <= limit:
            out.append(block)
            used += len(block)
        else:
            exceeded = True
            out.append(_stub(block, category))
    return "".join(out)
_STORE_CHUNK = 100  # diff bodies per store round-trip (~30KB avg keeps a chunk a few MB)


def _store_load(store: Store, head_sha: str) -> str | None:
    try:
        return store.load_diff(head_sha)
    except Exception as e:
        _log.warning("diff store read failed for %s: %s", head_sha[:12], e)
        return None


def _store_save(store: Store, rows: list[tuple[str, int | None, str]]) -> None:
    try:
        for i in range(0, len(rows), _STORE_CHUNK):
            store.save_diffs_many(rows[i:i + _STORE_CHUNK])
    except Exception as e:
        _log.warning("diff store write failed for %d row(s): %s", len(rows), e)


def _fetch_changed_paths(pr: int) -> list[str] | None:
    """Every changed path from GitHub's paginated per-file listing, or None
    when the listing is unavailable."""
    return gh.pr_changed_paths(pr)


def changed_paths(pr: int, head_sha: str | None,
                  diffs_dir: Path | None = None) -> list[str]:
    """File paths a PR changes, for the dependabot-bump scope check. Prefers the
    cached diff; falls back to the per-file listing (paths only, no patch) for PRs
    whose diff was never fetched."""
    diff = (diffs_dir or DIFFS) / f"{head_sha}.diff"
    if diff.exists() and diff.stat().st_size < MAX_DIFF_BYTES:
        return re.findall(r"^diff --git a/.+ b/(.+)$",
                          diff.read_text(errors="replace"), re.M)
    # A body at or past the cap is not trusted to carry every file header.
    # Fall back to the complete paginated listing rather than treating the
    # bounded summarizer cache as a complete manifest.
    return _fetch_changed_paths(pr) or []


def _listing_diff(files: list[dict]) -> str:
    """A diff rebuilt from GitHub's per-file listing: each file's header, a
    `# <status>: +<adds> -<dels>` line, and its patch when GitHub returned one."""
    parts = []
    for f in files:
        parts.append(f"diff --git a/{f['filename']} b/{f['filename']}")
        parts.append(f"# {f.get('status', '?')}: +{f.get('additions', 0)} -{f.get('deletions', 0)}")
        if f.get("patch"):
            parts.append(f["patch"])
    return "\n".join(parts)


def _synthesize_diff(pr: int) -> str | None:
    """GitHub refuses .diff for PRs over 20k lines (HTTP 406). Rebuild one from
    the per-file listing; files past GitHub's per-file patch limit appear as
    headers with +/- counts only."""
    files = gh.pr_files(pr)
    if files is None:
        return None
    return _listing_diff(files) or None


def _gh_pr_diff(pr: int) -> str | None:
    """PR `pr`'s unified diff from `gh pr diff`, or None when GitHub refuses it."""
    res = subprocess.run(["gh", "pr", "diff", str(pr), "--repo", settings.repo()],
                         capture_output=True, text=True, timeout=120,
                         env=operator_env())
    return res.stdout if res.returncode == 0 else None


# GitHub's per-file listing returns at most this many files.
LISTING_MAX_FILES = 3000

# A line directly under a file's `diff --git` header that starts "# " is a
# `bound` stub or a `_listing_diff` entry; a diff GitHub returns never has one.
_PARTIAL_FILE = re.compile(r"^diff --git [^\n]*\n# ", re.M)


def is_complete(text: str) -> bool:
    """Whether a diff carries every file's patch as `gh pr diff` returned it:
    no file reduced to a `bound` stub and none rebuilt from the per-file
    listing. A copy at or past the cap is not trusted to be whole: a whole
    diff that long reads the same as one cut there."""
    return len(text) < MAX_DIFF_BYTES and not _PARTIAL_FILE.search(text)


@dataclass(frozen=True)
class CompleteDiff:
    """A PR's whole diff as GitHub returns it. `unread` names what GitHub
    withheld: each file it returned no whole patch for though the file adds
    lines, and the files past the listing's limit when it was reached."""
    text: str
    unread: tuple[str, ...]


def _patch_withheld(f: dict) -> bool:
    """Whether a per-file listing entry adds lines its patch does not carry."""
    patch = f.get("patch") or ""
    carried = sum(1 for line in patch.split("\n") if line.startswith("+"))
    return int(f.get("additions") or 0) > carried


def fetch_complete(pr: int) -> CompleteDiff | None:
    """PR `pr`'s whole diff, read from GitHub for one reader and never cached:
    `gh pr diff`, else the per-file listing. None when GitHub answers
    neither."""
    try:
        text = _gh_pr_diff(pr)
    except subprocess.TimeoutExpired:
        text = None
    if text is not None:
        return CompleteDiff(text, ())
    return fetch_listing(pr)


def fetch_listing(pr: int) -> CompleteDiff | None:
    """PR `pr`'s diff rebuilt from GitHub's per-file listing at its current
    head, naming in `unread` what the listing withheld. None when GitHub does
    not answer."""
    files = gh.pr_files(pr)
    if files is None:
        return None
    unread = [f["filename"] for f in files if _patch_withheld(f)]
    if len(files) >= LISTING_MAX_FILES:
        unread.append(f"every file past GitHub's {LISTING_MAX_FILES:,}-file listing")
    return CompleteDiff(_listing_diff(files), tuple(unread))


def fetch_diff_paths(pr: int, head_sha: str, diffs_dir: Path | None = None,
                     store: Store | None = None) -> list[str] | None:
    """Cache the bounded diff and return paths from the complete response.

    Paths are extracted before the cache is capped, so callers deriving
    whole-PR signals such as `has_tests` do not miss files beyond the
    summarizer cache limit. A store hit carries the already-capped body, so a
    body at the cap falls back to the per-file listing the same way a capped
    local file does. None means GitHub supplied neither a diff nor a per-file
    fallback.
    """
    d = diffs_dir or DIFFS
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{head_sha}.diff"
    if path.exists():
        if path.stat().st_size < MAX_DIFF_BYTES:
            return diffpaths.changed_paths(path.read_text(errors="replace"))
        return _fetch_changed_paths(pr)
    if store is not None:
        body = _store_load(store, head_sha)
        if body is not None:
            path.write_text(body)
            if len(body) < MAX_DIFF_BYTES:
                return diffpaths.changed_paths(body)
            return _fetch_changed_paths(pr)
    text = _gh_pr_diff(pr)
    if text is None:
        text = _synthesize_diff(pr)
    if text is None:
        return None
    paths = diffpaths.changed_paths(text)
    bounded = bound(text)
    path.write_text(bounded)
    if store is not None:
        _store_save(store, [(head_sha, pr, bounded)])
    return paths


def fetch_diff(pr: int, head_sha: str, diffs_dir: Path | None = None,
               store: Store | None = None) -> bool:
    return fetch_diff_paths(pr, head_sha, diffs_dir, store=store) is not None


def fetch_diffs(manifest: list[DiffManifestItem], workers: int = 8,
                store: Store | None = None,
                diffs_dir: Path | None = None) -> tuple[int, int]:
    """Fetch every manifest entry's diff into the local cache. With a store,
    heads it already holds are pulled in bulk first (no GitHub calls for
    them); the rest fetch from GitHub and write through to the store."""
    d = diffs_dir or DIFFS
    if store is not None:
        wanted = [m.head_sha for m in manifest
                  if m.head_sha and not (d / f"{m.head_sha}.diff").exists()]
        d.mkdir(parents=True, exist_ok=True)
        lookup = progress.Progress("looking up", len(wanted), "diffs in the shared store",
                                   one="diff in the shared store")
        found = 0
        for i in range(0, len(wanted), _STORE_CHUNK):
            batch = wanted[i:i + _STORE_CHUNK]
            try:
                pulled = store.load_diffs(batch)
            except Exception as e:
                _log.warning("diff store bulk read failed: %s", e)
                break
            for sha, body in pulled.items():
                (d / f"{sha}.diff").write_text(body)
            found += len(pulled)
            lookup.advance(len(batch))
        lookup.finish(f"{found:,} found")
    remote = {i for i, m in enumerate(manifest) if not (d / f"{m.head_sha}.diff").exists()}
    fetching = progress.Progress("fetching", len(remote), "diffs from GitHub",
                                 one="diff from GitHub")
    ok = bad = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = ex.map(lambda m: fetch_diff(m.pr, m.head_sha, diffs_dir, store), manifest)
        for i, good in enumerate(results):
            ok, bad = (ok + 1, bad) if good else (ok, bad + 1)
            if i in remote:
                fetching.advance()
    fetching.finish(f"{bad:,} failed" if bad else None)
    return ok, bad
