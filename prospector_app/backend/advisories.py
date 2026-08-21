"""Repository security advisories, folded into the app: a read-only projection
over the advisory store for the 🛡️ Alerts tab's Advisories sub-view. There is
no upstream write path for advisories.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from alert_triage.advisory_store import advisory_id
from prospector_app.backend import advisory_data

if TYPE_CHECKING:
    from alert_triage.advisory_model import Advisory

STORE_ROOT: Path | None = None
_synced_store_root: Path | None = None


def _sync_store_root() -> None:
    global _synced_store_root
    normalized = Path(STORE_ROOT) if STORE_ROOT is not None else None
    if normalized != _synced_store_root:
        advisory_data.set_store_root(normalized)
        _synced_store_root = normalized


def _store_pr_states() -> tuple[dict[int, str], bool]:
    from prospector_app.backend import data
    if data.snapshot_loading():
        return {}, True
    return {n: pr.state for n, pr in data.prs().items() if pr.state}, False


def _row(a: Advisory, pr_states: dict[int, str]) -> dict:
    fs = a.fix_scan or {}
    links = []
    for c in a.candidates:
        link = dict(c)
        if c.get("kind") == "pr" and c.get("number") in pr_states:
            link["state"] = pr_states[c["number"]]
        links.append(link)
    return {
        "id": a.id,
        "ghsa_id": a.ghsa_id,
        "state": a.state,
        "severity": a.severity,
        "summary": a.summary,
        "reporter": a.reporter,
        "cve_id": a.cve_id,
        "created_at": a.created_at,
        "updated_at": a.updated_at,
        "html_url": a.html_url,
        "verdict": a.verdict,
        "by": fs.get("by"),
        "duplicate_of": a.duplicate_of,
        "fix_commit": a.fix_commit,
        "evidence": fs.get("evidence"),
        "links": links,
        "link_count": len(links),
    }


def list_advisories() -> tuple[list[dict], bool]:
    """Every advisory, newest update first, plus whether PR-state hydration of
    the link chips is still pending behind the cold PR-snapshot load."""
    _sync_store_root()
    pr_states, loading = _store_pr_states()
    rows = [_row(a, pr_states) for a in advisory_data.advisories().values()]
    rows.sort(key=lambda r: r["updated_at"] or "", reverse=True)
    return rows, loading


def get_advisory(ghsa: str) -> dict | None:
    _sync_store_root()
    try:
        i = advisory_id(ghsa)
    except ValueError:
        return None
    a = advisory_data.advisories().get(i)
    if a is None:
        return None
    row = _row(a, _store_pr_states()[0])
    meta = a.section("meta") or {}
    row["description"] = meta.get("description") or ""
    row["cwe_ids"] = meta.get("cwe_ids") or []
    row["vulnerable_range"] = meta.get("vulnerable_range")
    row["patched_versions"] = meta.get("patched_versions")
    row["fix_scan"] = a.fix_scan
    return row


_SEVERITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0, "unknown": -1}
_SORT_KEYS = {
    "ghsa": lambda r: r["ghsa_id"],
    "state": lambda r: r["state"] or "",
    "severity": lambda r: _SEVERITY_RANK.get(r["severity"] or "", -2),
    "summary": lambda r: (r["summary"] or "").lower(),
    "reporter": lambda r: (r["reporter"] or "").lower(),
    "verdict": lambda r: r["verdict"] or "",
    "links": lambda r: r["link_count"],
    "created": lambda r: r["created_at"] or "",
    "updated": lambda r: r["updated_at"] or "",
}
_DEFAULT_DESC = {"severity", "updated", "created", "links"}


def query_advisories(q: str = "", sort: str | None = None, direction: str | None = None,
                     state: str | list[str] | None = None, verdict: str | None = None,
                     offset: int = 0, limit: int = 50) -> dict:
    """Paginated table query. `state` is one value or a list (OR'd; "all"/None
    = everything); `verdict` filters the fix-scan verdict, "none" selecting
    unscanned; `q` is a case-insensitive substring over ghsa, summary,
    reporter, and CVE id."""
    rows, loading = list_advisories()
    if state and state != "all":
        wanted = state if isinstance(state, list) else [state]
        rows = [r for r in rows if r["state"] in wanted]
    if verdict:
        rows = [r for r in rows if (r["verdict"] or "none") == verdict]
    needle = q.strip().lower()
    if needle:
        rows = [r for r in rows
                if any(needle in (r[k] or "").lower()
                       for k in ("ghsa_id", "summary", "reporter", "cve_id"))]
    key = _SORT_KEYS.get(sort or "", _SORT_KEYS["updated"])
    reverse = (direction == "desc" if direction in ("asc", "desc")
               else (sort or "updated") in _DEFAULT_DESC)
    rows.sort(key=lambda r: (key(r), r["id"]), reverse=reverse)
    return {"items": rows[offset:offset + limit], "total": len(rows),
            "offset": offset, "limit": limit, "pr_states_loading": loading}
