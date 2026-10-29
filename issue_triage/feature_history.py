"""The taste digest: plain files on how maintainers treated past feature
requests, read by the feature-request screener."""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path

from issue_triage import config
from issue_triage.issue_model import Issue
from pipeline import gh, profile, settings

HISTORY_MAX = 150
HISTORY_MAX_AGE = timedelta(hours=24)
BODY_MAX = 2000
COMMENT_MAX = 1500
_MAINTAINER = {"OWNER", "MEMBER", "COLLABORATOR"}
_FETCHED = ".fetched"
_DECISIONS = "decisions.md"
# `files`: each closed-<n>.md file's issue number and the updated_at it was
# written at; `since`: the updated_at the next listing starts from.
_INDEX = ".index.json"


def history_dir() -> Path:
    return settings.verify_scratch() / "feature-screen" / "history"


def render_closed(raw: dict, comments: list[dict]) -> str:
    labels = ", ".join(lb.get("name", "") for lb in raw.get("labels") or [])
    parts = [f"# #{raw['number']} {raw.get('title') or ''}",
             f"Closed as: {raw.get('state_reason') or 'unknown'}",
             f"Labels: {labels}",
             "", (raw.get("body") or "")[:BODY_MAX]]
    for c in comments:
        if c.get("author_association") in _MAINTAINER:
            parts += ["", f"## Maintainer comment by {(c.get('user') or {}).get('login', '?')}",
                      (c.get("body") or "")[:COMMENT_MAX]]
    return "\n".join(parts) + "\n"


def render_decisions(issues: Iterable[Issue]) -> str:
    """The newest HISTORY_MAX maintainer decisions, newest first."""
    decided = sorted(((d, issue) for issue in issues if (d := issue.feature_decision)),
                     key=lambda p: str(p[0].get("at") or ""), reverse=True)
    lines = ["# Feature-request decisions in this deployment", ""]
    for d, issue in decided[:HISTORY_MAX]:
        line = f"#{issue.number} {issue.title or ''}: {d['decision']} (by {d.get('by', '?')})"
        if d.get("guidance"):
            line += f" - {d['guidance']}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _fetch_closed(since: str | None) -> list[dict] | None:
    """The most recently closed feature requests across the feature labels,
    only those updated at or after `since` when given, or None when any
    listing fails."""
    found: dict[int, dict] = {}
    after = f"&since={urllib.parse.quote(since, safe='')}" if since else ""
    for label in profile.active().feature_labels:
        quoted = urllib.parse.quote(label, safe="")
        rows = gh.gh_list(f"repos/{config.repo()}/issues?state=closed&labels={quoted}"
                          f"&sort=updated&per_page=100{after}")
        if rows is None:
            return None
        for row in rows:
            if "pull_request" not in row:
                found[row["number"]] = row
    return list(found.values())


def _write_if_changed(path: Path, text: str) -> None:
    if not path.exists() or path.read_text() != text:
        path.write_text(text)


def _sync_closed(d: Path) -> bool:
    """Bring the closed-<n>.md files up to date: list only what was updated
    since the last pass, fetch comments only for an issue whose updated_at
    moved since its file was written, and keep the newest HISTORY_MAX. An
    issue whose comments could not be fetched holds the cursor at its
    updated_at, so the next pass lists it again. Whether the listing
    succeeded."""
    index_path = d / _INDEX
    try:
        saved = json.loads(index_path.read_text())
        index: dict[str, str] = dict(saved["files"])
        since: str | None = saved["since"]
    except (OSError, ValueError, KeyError, TypeError):
        index, since = {}, None
    rows = _fetch_closed(since)
    if rows is None:
        return False
    listed = {str(r["number"]): r for r in rows}
    updated = {**index, **{n: str(r.get("updated_at") or "") for n, r in listed.items()}}
    keep = dict(sorted(updated.items(), key=lambda kv: kv[1], reverse=True)[:HISTORY_MAX])
    failed: list[str] = []
    for n, row in listed.items():
        path = d / f"closed-{n}.md"
        if n not in keep or (index.get(n) == keep[n] and path.exists()):
            continue
        comments = gh.issue_comments(int(n))
        if comments is None:
            failed.append(keep[n])
            if n in index:
                keep[n] = index[n]
            else:
                del keep[n]
            continue
        path.write_text(render_closed(row, comments))
    for old in d.glob("closed-*.md"):
        if old.name.removeprefix("closed-").removesuffix(".md") not in keep:
            old.unlink()
    cursor = min(failed) if failed else max(updated.values(), default=since)
    index_path.write_text(json.dumps({"since": cursor, "files": keep}))
    return True


def refresh(issues: Iterable[Issue], *, force: bool = False) -> Path:
    """The digest directory, its closed requests re-synced once per
    HISTORY_MAX_AGE (or when `force`d) and decisions.md rewritten when it
    would change."""
    d = history_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = d / _FETCHED
    fresh = stamp.exists() and time.time() - stamp.stat().st_mtime < HISTORY_MAX_AGE.total_seconds()
    if (force or not fresh) and _sync_closed(d):
        stamp.touch()
    _write_if_changed(d / _DECISIONS, render_decisions(issues))
    return d


def precedents() -> set[int]:
    """Issue numbers the screener may cite: the closed-<n>.md files and the
    issues listed in decisions.md."""
    d = history_dir()
    nums = {int(m.group(1)) for p in d.glob("closed-*.md") if (m := re.fullmatch(r"closed-(\d+)\.md", p.name))}
    decisions = d / _DECISIONS
    if decisions.exists():
        nums |= {int(n) for n in re.findall(r"^#(\d+) ", decisions.read_text(), re.M)}
    return nums
