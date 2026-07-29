"""Machine-local PR diff cache and the bounded, read-only GitHub fetch into it.

One file per PR head (`cache/diffs/<head_sha>.diff`), capped at MAX_DIFF_BYTES.
Fetches are read-only against GitHub: `gh pr diff`, falling back to a diff
synthesized from the paginated per-file listing when GitHub refuses the diff
outright (HTTP 406 for PRs over 20k lines). The CLUSTER wave's fetch-diffs
stage and the threat scan's fetch-missing step both fetch through here.

Every function takes an optional `diffs_dir` override (tests, alternate
caches); None means the canonical DIFFS directory.
"""
from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import diffpaths
from pipeline.settings import REPO

if TYPE_CHECKING:
    from pipeline.wire import DiffManifestItem

DIFFS = Path(__file__).resolve().parent / "cache" / "diffs"
MAX_DIFF_BYTES = 200_000  # summarizers don't need megadiffs


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


def fetch_diff_paths(pr: int, head_sha: str,
                     diffs_dir: Path | None = None) -> list[str] | None:
    """Cache the bounded diff and return paths from the complete response.

    Paths are extracted before the cache is capped, so callers deriving
    whole-PR signals such as `has_tests` do not miss files beyond the
    summarizer cache limit. None means GitHub supplied neither a diff nor a
    per-file fallback.
    """
    d = diffs_dir or DIFFS
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{head_sha}.diff"
    if path.exists():
        if path.stat().st_size < MAX_DIFF_BYTES:
            return diffpaths.changed_paths(path.read_text(errors="replace"))
        return _fetch_changed_paths(pr)
    res = subprocess.run(["gh", "pr", "diff", str(pr), "--repo", REPO],
                         capture_output=True, text=True, timeout=120)
    text = res.stdout if res.returncode == 0 else _synthesize_diff(pr)
    if text is None:
        return None
    paths = diffpaths.changed_paths(text)
    path.write_text(text[:MAX_DIFF_BYTES])
    return paths


def fetch_diff(pr: int, head_sha: str, diffs_dir: Path | None = None) -> bool:
    return fetch_diff_paths(pr, head_sha, diffs_dir) is not None


def fetch_diffs(manifest: list[DiffManifestItem], workers: int = 8) -> tuple[int, int]:
    ok = bad = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for good in ex.map(lambda m: fetch_diff(m.pr, m.head_sha), manifest):
            ok, bad = (ok + 1, bad) if good else (ok, bad + 1)
    return ok, bad
