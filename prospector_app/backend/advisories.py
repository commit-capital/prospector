"""Repository security advisories, folded into the app: a read-only projection
over the advisory store for the 🛡️ Alerts tab's Advisories sub-view. There is
no upstream write path for advisories.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from alert_triage import advisory_dups
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


def _dup_pointers() -> dict[str, str]:
    """Every duplicate-marked advisory's GHSA to its target's."""
    return {a.ghsa_id: a.duplicate_of for a in advisory_data.advisories().values()
            if a.verdict == "duplicate" and a.duplicate_of}


def list_advisories() -> tuple[list[dict], bool]:
    """Every advisory, newest update first, each row carrying `canonical` (its
    dup group's resolved lead — itself when it is the lead), plus whether
    PR-state hydration of the link chips is still pending behind the cold
    PR-snapshot load."""
    _sync_store_root()
    pr_states, loading = _store_pr_states()
    rows = [_row(a, pr_states) for a in advisory_data.advisories().values()]
    pointers = _dup_pointers()
    for r in rows:
        r["canonical"] = advisory_dups.canonical_of(pointers, r["ghsa_id"])
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
    row["canonical"] = advisory_dups.canonical_of(_dup_pointers(), row["ghsa_id"])
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


def _collapse_dups(rows: list[dict]) -> list[dict]:
    """One row per dup group: each row whose resolved canonical is another row
    in the set folds under that row as `dup_rows` (kept in the incoming sort
    order); every kept row carries `dup_count`. A member whose canonical the
    filters removed stays a top-level row."""
    by_ghsa = {r["ghsa_id"]: r for r in rows}
    leads: list[dict] = []
    for r in rows:
        canon = r["canonical"]
        if canon != r["ghsa_id"] and canon in by_ghsa:
            by_ghsa[canon].setdefault("dup_rows", []).append(r)
        else:
            leads.append(r)
    for r in leads:
        r["dup_count"] = len(r.get("dup_rows") or [])
    return leads


def query_advisories(q: str = "", sort: str | None = None, direction: str | None = None,
                     state: str | list[str] | None = None,
                     severity: str | list[str] | None = None,
                     verdict: str | None = None, collapse_dups: bool = False,
                     offset: int = 0, limit: int = 50) -> dict:
    """Paginated table query. `state` is one value or a list (OR'd; "all" in
    either form, or None, = everything); `severity` accepts one value or a
    list (OR'd); `verdict` filters the fix-scan verdict, "none" selecting
    unscanned; `collapse_dups` folds each dup group under its canonical row,
    so a group counts and pages as one row; `q` is a case-insensitive
    substring over ghsa, summary, reporter, and CVE id. With no `sort` (or an
    unknown one) rows order by severity, most severe first, ties oldest
    first — an open critical advisory always leads the list."""
    rows, loading = list_advisories()
    wanted = [s for s in (state if isinstance(state, list) else [state]) if s]
    if wanted and "all" not in wanted:
        rows = [r for r in rows if r["state"] in wanted]
    if severity:
        wanted_sev = severity if isinstance(severity, list) else [severity]
        rows = [r for r in rows if r["severity"] in wanted_sev]
    if verdict:
        rows = [r for r in rows if (r["verdict"] or "none") == verdict]
    needle = q.strip().lower()
    if needle:
        rows = [r for r in rows
                if any(needle in (r[k] or "").lower()
                       for k in ("ghsa_id", "summary", "reporter", "cve_id"))]
    key_name = sort if sort in _SORT_KEYS else "severity"
    reverse = (direction == "desc" if direction in ("asc", "desc")
               else key_name in _DEFAULT_DESC)
    # Two stable passes: whatever the sort column, equal rows order oldest
    # first, so "severity" reads as severity-then-age.
    rows.sort(key=lambda r: (r["created_at"] or "", r["id"]))
    rows.sort(key=_SORT_KEYS[key_name], reverse=reverse)
    if collapse_dups:
        rows = _collapse_dups(rows)
    return {"items": rows[offset:offset + limit], "total": len(rows),
            "offset": offset, "limit": limit, "pr_states_loading": loading}
