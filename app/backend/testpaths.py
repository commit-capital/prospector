"""Single source of truth for change classification and diff size splitting.

The app shows the *real* surface area of a change: the non-test split flags
PRs that delete more test code than they add (#17, #22), and the effective-LOC
breakdown strips machine-generated artifacts so a huge raw diffstat reads as
the lines a human actually wrote and a reviewer reads.

The test-path matcher and the diff→paths parse live in `pipeline/diffpaths.py`
(the pipeline's security driver classifies risk tiers with them too); this
module re-exports them for the app. Artifact classification reads the
active repository profile's `artifact_rules`; the generic default covers
lockfiles, locale bundles, vendored/build output, and generated code.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import profile
from pipeline.diffpaths import changed_paths, is_test_path
from pipeline.profile import ARTIFACT_CATEGORIES

if TYPE_CHECKING:
    from pipeline.model import Pr


_LOC_CATEGORIES = ("source", "test", *ARTIFACT_CATEGORIES)


def classify_path(path: str) -> str:
    """Classify a changed file into 'source' (reviewable), 'test', or one of the
    generated-artifact categories in ARTIFACT_CATEGORIES. Artifact rules win over
    the test/source split, and the first matching artifact rule wins."""
    p = (path or "").strip()
    if not p:
        return "source"
    for rule in profile.active().artifact_rules:
        if re.search(rule.pattern, p, re.IGNORECASE):
            return rule.category
    return "test" if is_test_path(p) else "source"


def _shape_breakdown(buckets: dict[str, dict]) -> dict:
    by: dict[str, dict] = {}
    raw = effective = 0
    for c in _LOC_CATEGORIES:
        b = buckets[c]
        total = b["additions"] + b["deletions"]
        raw += total
        if c in ("source", "test"):
            effective += total
        if b["files"]:
            by[c] = {"additions": b["additions"], "deletions": b["deletions"], "files": len(b["files"])}
    art = {c: by[c] for c in ARTIFACT_CATEGORIES if c in by}
    dominant = max(art, key=lambda c: art[c]["additions"] + art[c]["deletions"], default=None)
    return {"effective": effective, "raw": raw, "artifact": raw - effective,
            "dominant_artifact": dominant, "by_category": by}


def loc_breakdown(text: str) -> dict:
    """Per-category +/- line counts for a unified diff, plus `effective` (source +
    test lines), `raw` (all lines), `artifact` (raw − effective), and
    `dominant_artifact`. See ARTIFACT_CATEGORIES for what's stripped."""
    buckets = {c: _empty() for c in _LOC_CATEGORIES}
    cls: dict[str, str] = {}
    cur: str | None = None
    for line in (text or "").splitlines():
        if line.startswith("diff --git "):
            tok = line.split(" ")[-1]
            cur = tok[2:] if tok.startswith("b/") else tok
            continue
        if line.startswith("+++ "):
            p = line[4:].strip()
            if p and p != "/dev/null":
                cur = p[2:] if p.startswith("b/") else p
            continue
        if line.startswith("--- ") or line.startswith("@@") or cur is None:
            continue
        if line[:1] in ("+", "-"):
            cat = cls.get(cur) or cls.setdefault(cur, classify_path(cur))
            b = buckets[cat]
            b["additions" if line[0] == "+" else "deletions"] += 1
            b["files"].add(cur)
    return _shape_breakdown(buckets)


def loc_breakdown_from_files(files: list[dict]) -> dict:
    """The same breakdown from a GitHub `pulls/{n}/files` list (each item
    {filename, additions, deletions}) — no diff text needed. This is the path a
    size-backfill ingest uses so the whole queue gets effective LOC, not just the
    PRs whose full diff happens to be cached."""
    buckets = {c: _empty() for c in _LOC_CATEGORIES}
    for f in files or []:
        name = f.get("filename") or ""
        b = buckets[classify_path(name)]
        b["additions"] += int(f.get("additions") or 0)
        b["deletions"] += int(f.get("deletions") or 0)
        if name:
            b["files"].add(name)
    return _shape_breakdown(buckets)


def _empty() -> dict:
    return {"additions": 0, "deletions": 0, "files": set()}


def split_unified_diff(text: str) -> dict:
    """Split a unified diff's added/removed line counts into test vs non-test.

    Returns {"test": {additions, deletions, files}, "non_test": {…},
             "removes_tests": bool}. `files` is a count of distinct files.
    """
    cur: str | None = None
    test, non = _empty(), _empty()

    def bucket(path: str) -> dict:
        return test if is_test_path(path) else non

    for line in (text or "").splitlines():
        if line.startswith("diff --git "):
            tok = line.split(" ")[-1]
            cur = tok[2:] if tok.startswith("b/") else tok
            continue
        if line.startswith("+++ "):
            p = line[4:].strip()
            if p and p != "/dev/null":
                cur = p[2:] if p.startswith("b/") else p
            continue
        if line.startswith("--- ") or line.startswith("@@"):
            continue
        if cur is None:
            continue
        if line.startswith("+"):
            b = bucket(cur); b["additions"] += 1; b["files"].add(cur)
        elif line.startswith("-"):
            b = bucket(cur); b["deletions"] += 1; b["files"].add(cur)

    def shape(d: dict) -> dict:
        return {"additions": d["additions"], "deletions": d["deletions"], "files": len(d["files"])}

    return {
        "test": shape(test),
        "non_test": shape(non),
        "removes_tests": test["deletions"] > test["additions"],
    }


def cached_diff_text(rec: Pr | None, diff_cache: Path) -> str | None:
    sha = rec.head_sha if rec is not None else None
    if not sha:
        return None
    cached = diff_cache / f"{sha}.diff"
    if not cached.exists():
        return None
    try:
        return cached.read_text()
    except Exception:
        return None


def diff_facets(text: str) -> dict:
    """All diff-derived facets from a unified diff's text, in one parse: the
    test/non-test size split, the effective-LOC breakdown (diff minus generated
    artifacts), and the list of changed file paths."""
    return {"size_split": split_unified_diff(text),
            "loc_breakdown": loc_breakdown(text),
            "changed_paths": changed_paths(text)}
