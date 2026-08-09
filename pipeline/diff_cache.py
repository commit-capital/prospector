"""Machine-local PR diff cache and the bounded, read-only GitHub fetch into it.

One file per PR head (`cache/diffs/<head_sha>.diff`), capped at MAX_DIFF_BYTES.
Fetches are read-only against GitHub: `gh pr diff`, falling back to a diff
synthesized from the paginated per-file listing when GitHub refuses the diff
outright (HTTP 406 for PRs over 20k lines). The CLUSTER wave's fetch-diffs
stage and the threat scan's fetch-missing step both fetch through here.

With a `store` supplied, fetches read through the shared `diffs` table: local
file, then the store, then GitHub — writing back at each level, so one
machine's fetch spares every other machine the download. A head's diff never
changes, so a store row is fresh by construction. Store failures degrade to
the direct GitHub fetch (it is a cache, not a gate), logged as warnings.

Every function takes an optional `diffs_dir` override (tests, alternate
caches); None means the canonical DIFFS directory.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import diffpaths
from pipeline.settings import REPO

if TYPE_CHECKING:
    from pipeline.store import Store
    from pipeline.wire import DiffManifestItem

_log = logging.getLogger(__name__)

DIFFS = Path(__file__).resolve().parent / "cache" / "diffs"
MAX_DIFF_BYTES = 200_000  # summarizers don't need megadiffs
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
    res = subprocess.run(["gh", "api", f"repos/{REPO}/pulls/{pr}/files",
                          "--paginate", "--jq", ".[].filename"],
                         capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        return None
    return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]


def changed_paths(pr: int, head_sha: str | None,
                  diffs_dir: Path | None = None) -> list[str]:
    """File paths a PR changes, for the dependabot-bump scope check. Prefers the
    cached diff; falls back to the per-file listing (paths only, no patch) for PRs
    whose diff was never fetched."""
    diff = (diffs_dir or DIFFS) / f"{head_sha}.diff"
    if diff.exists() and diff.stat().st_size < MAX_DIFF_BYTES:
        return re.findall(r"^diff --git a/.+ b/(.+)$",
                          diff.read_text(errors="replace"), re.M)
    # A cache file exactly at the cap may have lost trailing file headers.
    # Fall back to the complete paginated listing rather than treating the
    # bounded summarizer cache as a complete manifest.
    return _fetch_changed_paths(pr) or []


def _synthesize_diff(pr: int) -> str | None:
    """GitHub refuses .diff for PRs over 20k lines (HTTP 406). Rebuild one from
    the per-file listing; files past GitHub's per-file patch limit appear as
    headers with +/- counts only."""
    res = subprocess.run(["gh", "api", f"repos/{REPO}/pulls/{pr}/files",
                          "--paginate", "--jq", ".[]"],
                         capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        return None
    parts = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        try:
            f = json.loads(line)
        except json.JSONDecodeError:
            return None
        parts.append(f"diff --git a/{f['filename']} b/{f['filename']}")
        parts.append(f"# {f.get('status', '?')}: +{f.get('additions', 0)} -{f.get('deletions', 0)}")
        if f.get("patch"):
            parts.append(f["patch"])
    return "\n".join(parts) if parts else None


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
    res = subprocess.run(["gh", "pr", "diff", str(pr), "--repo", REPO],
                         capture_output=True, text=True, timeout=120)
    text = res.stdout if res.returncode == 0 else _synthesize_diff(pr)
    if text is None:
        return None
    paths = diffpaths.changed_paths(text)
    path.write_text(text[:MAX_DIFF_BYTES])
    if store is not None:
        _store_save(store, [(head_sha, pr, text[:MAX_DIFF_BYTES])])
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
        for i in range(0, len(wanted), _STORE_CHUNK):
            try:
                pulled = store.load_diffs(wanted[i:i + _STORE_CHUNK])
            except Exception as e:
                _log.warning("diff store bulk read failed: %s", e)
                break
            for sha, body in pulled.items():
                (d / f"{sha}.diff").write_text(body)
    ok = bad = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for good in ex.map(lambda m: fetch_diff(m.pr, m.head_sha, diffs_dir, store),
                           manifest):
            ok, bad = (ok + 1, bad) if good else (ok, bad + 1)
    return ok, bad
