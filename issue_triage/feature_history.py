"""The taste digest: plain files on how maintainers treated past feature
requests, read by the feature-request screener."""
from __future__ import annotations

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
    lines = ["# Feature-request decisions in this deployment", ""]
    for issue in issues:
        d = issue.feature_decision
        if not d:
            continue
        line = f"#{issue.number} {issue.title or ''}: {d['decision']} (by {d.get('by', '?')})"
        if d.get("guidance"):
            line += f" - {d['guidance']}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _fetch_closed() -> list[dict] | None:
    """The most recently closed feature requests across the feature labels, or
    None when any listing fails."""
    found: dict[int, dict] = {}
    for label in profile.active().feature_labels:
        quoted = urllib.parse.quote(label, safe="")
        rows = gh.gh_list(f"repos/{config.repo()}/issues?state=closed&labels={quoted}"
                          "&sort=updated&per_page=100")
        if rows is None:
            return None
        for row in rows:
            if "pull_request" not in row:
                found[row["number"]] = row
    newest = sorted(found.values(), key=lambda r: r.get("updated_at") or "", reverse=True)
    return newest[:HISTORY_MAX]


def refresh(issues: Iterable[Issue], *, force: bool = False) -> Path:
    d = history_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = d / _FETCHED
    fresh = stamp.exists() and time.time() - stamp.stat().st_mtime < HISTORY_MAX_AGE.total_seconds()
    if force or not fresh:
        rows = _fetch_closed()
        if rows is not None:
            texts = {f"closed-{r['number']}.md": render_closed(r, gh.issue_comments(r["number"]) or [])
                     for r in rows}
            for old in d.glob("closed-*.md"):
                if old.name not in texts:
                    old.unlink()
            for name, text in texts.items():
                (d / name).write_text(text)
            stamp.touch()
    (d / _DECISIONS).write_text(render_decisions(issues))
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
