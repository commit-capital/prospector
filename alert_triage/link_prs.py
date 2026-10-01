"""Join alerts to candidate fixing PRs, deterministically.

Three signals, strongest evidence first: (a) `manifest-bump` — a dependabot
alert matched to a PR whose title is a dependency bump of the same package;
(b) `diff-overlap` — a code-scanning alert matched to a PR whose cached diff
touches the alert's file; (c) `text-ref` — any source matched to a PR whose
title/body mentions the alert's rule id, GHSA/CVE id, package, or secret type.
Direct matches (a, b) are always kept; text-refs fill the remainder of
LINK_CAP, resolved PRs first (a merged mention is the durable fix evidence).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.store import Store

LINK_CAP = 8

# Dependency-bump PR titles ("Bump lodash from 4.17.20 to 4.17.30", with or
# without a conventional-commit prefix).
_BUMP = re.compile(r"\bbump\s+(?P<pkg>\S+)\s+from\s+\S+\s+to\s+(?P<ver>\S+)", re.I)

# Identifiers shorter than this are too generic to count as a text reference.
_MIN_IDENT = 4

_STATE_RANK = {"merged": 0, "closed": 1, "open": 2}


def parse_bump(title: str) -> tuple[str, str] | None:
    """(package, new version) from a dependency-bump PR title, or None."""
    m = _BUMP.search(title or "")
    if not m:
        return None
    return m.group("pkg"), m.group("ver").rstrip(".")


def version_gte(a: str, b: str) -> bool | None:
    """Best-effort dotted-integer compare: True/False when both versions parse
    as dotted ints (leading 'v' tolerated), None when either doesn't."""
    def parse(v: str) -> tuple[int, ...] | None:
        parts = v.lstrip("vV").split(".")
        try:
            return tuple(int(p) for p in parts)
        except ValueError:
            return None
    pa, pb = parse(a), parse(b)
    if pa is None or pb is None:
        return None
    return pa >= pb


def _identifiers(meta: dict) -> list[str]:
    """The alert's searchable identity strings, longest first so the most
    specific mention wins ties."""
    idents = [meta.get("rule_id"), meta.get("ghsa_id"), meta.get("cve_id"),
              meta.get("package"), meta.get("secret_type")]
    out = [i for i in idents if isinstance(i, str) and len(i) >= _MIN_IDENT]
    return sorted(set(out), key=len, reverse=True)


def _diff_touches(diff: str, path: str) -> bool:
    return f"+++ b/{path}" in diff or f"--- a/{path}" in diff


def candidates_for(meta: dict, prs: list[dict], diffs: dict[str, str]) -> list[dict]:
    """The PRs that may address this alert, strongest evidence per PR. `prs`
    rows carry number/title/body/state/head_sha; `diffs` maps head_sha to the
    cached diff text. Returns `[{"kind": "pr", "number", "how", "state"}]`,
    direct matches first, text-refs (resolved PRs first) filling up to
    LINK_CAP."""
    direct: list[dict] = []
    text: list[dict] = []
    idents = _identifiers(meta)
    package = (meta.get("package") or "").lower()
    path = meta.get("path")
    for pr in prs:
        title, body = pr.get("title") or "", pr.get("body") or ""
        entry = {"kind": "pr", "number": pr["number"], "state": pr.get("state", "open")}
        if meta.get("source") == "dependabot" and package:
            bump = parse_bump(title)
            if bump and bump[0].lower() == package:
                direct.append({**entry, "how": "manifest-bump", "bump_version": bump[1]})
                continue
        if meta.get("source") == "code-scanning" and path:
            diff = diffs.get(pr.get("head_sha") or "")
            if diff and _diff_touches(diff, path):
                direct.append({**entry, "how": "diff-overlap"})
                continue
        haystack = f"{title}\n{body}".lower()
        if any(i.lower() in haystack for i in idents):
            text.append({**entry, "how": "text-ref"})
    text.sort(key=lambda c: (_STATE_RANK.get(c["state"], 2), c["number"]))
    return direct + text[:max(0, LINK_CAP - len(direct))]


# The link rows read per store URL, so a sweep's alert and advisory ingests
# share one read of the PR store.
_ROWS: dict[str, list[dict]] = {}


def pr_corpus(store: Store | None = None,
              paths: Iterable[str] = ()) -> tuple[list[dict], dict[str, str]]:
    """The PR corpus the linkers match against — every open or merged PR's
    number, title, description, state and head — plus the cached diffs of the
    open PRs whose diff touches one of `paths`, the only diffs a diff-overlap
    link can use (merged-PR diffs are matched by the fixed-pass agent). The rows
    are read once per process per store."""
    from pipeline.store import Store as PrStore
    st = store or PrStore()
    key = str(st.engine.url)
    rows = _ROWS.get(key)
    if rows is None:
        rows = _ROWS[key] = st.link_rows()
    needles = sorted({f"+++ b/{p}" for p in paths} | {f"--- a/{p}" for p in paths})
    open_heads = [r["head_sha"] for r in rows if r["state"] == "open" and r["head_sha"]]
    diffs = st.load_diffs(open_heads, containing=needles) if needles and open_heads else {}
    return rows, diffs


def corpus_for(records: int, noun: str,
               paths: set[str] | None = None) -> tuple[list[dict], dict[str, str]]:
    """`pr_corpus` for linking `records` new or changed records, announced on
    stdout; empty when there is nothing to link."""
    if not records:
        return [], {}
    print(f"loading open and merged PRs to link {records} {noun} against…", flush=True)
    prs, diffs = pr_corpus(paths=paths or set())
    touched = (f"; {len(diffs)} open-PR diff{'s' if len(diffs) != 1 else ''} touch "
               f"the {len(paths)} alerted file{'s' if len(paths) != 1 else ''}"
               if paths else "")
    print(f"  {len(prs):,} PRs{touched}", flush=True)
    return prs, diffs
