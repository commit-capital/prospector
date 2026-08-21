"""GitHub security alerts, folded into the app.

A read-only projection over the alert store (alert_triage): normalized alert
rows with their fix-scan verdicts and candidate PR/issue links, the filters
and sorts the 🛡️ Alerts view uses, and the per-source availability probe.

Writes (dismiss/resolve) go through the app executor as the configured bot,
gated by alert_gates.dismiss_eligibility and logged like every other upstream
write (executor.dismiss_alert).
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from alert_triage import config as alert_config
from alert_triage.alert_gates import DISMISS_REASONS
from alert_triage.alert_store import alert_id
from prospector_app.backend import alert_data

if TYPE_CHECKING:
    from alert_triage.alert_model import Alert

STORE_ROOT: Path | None = None
_synced_store_root: Path | None = None


def _sync_store_root() -> None:
    global _synced_store_root
    normalized = Path(STORE_ROOT) if STORE_ROOT is not None else None
    if normalized != _synced_store_root:
        alert_data.set_store_root(normalized)
        _synced_store_root = normalized


def _store_pr_states() -> tuple[dict[int, str], bool]:
    """Current PR states for the link chips, plus True while the PR snapshot
    is still cold-loading. That load runs on a background thread and the
    states come back empty in the meantime, so an alerts read never pins a
    request thread on the full PR corpus."""
    from prospector_app.backend import data
    if data.snapshot_loading():
        return {}, True
    return {n: pr.state for n, pr in data.prs().items() if pr.state}, False


def _row(alert: Alert, pr_states: dict[int, str]) -> dict:
    meta = alert.section("meta") or {}
    fs = alert.fix_scan or {}
    links = []
    for c in alert.candidates:
        link = dict(c)
        if c.get("kind") == "pr" and c.get("number") in pr_states:
            link["state"] = pr_states[c["number"]]
        links.append(link)
    return {
        "id": alert.id,
        "source": alert.source,
        "number": alert.number,
        "state": alert.state,
        "raw_state": alert.raw_state,
        "severity": alert.severity,
        "title": alert.title,
        "rule_id": meta.get("rule_id"),
        "package": meta.get("package"),
        "ecosystem": meta.get("ecosystem"),
        "manifest_path": meta.get("manifest_path"),
        "secret_type": meta.get("secret_type"),
        "path": meta.get("path"),
        "start_line": meta.get("start_line"),
        "html_url": alert.html_url,
        "created_at": alert.created_at,
        "updated_at": alert.updated_at,
        "verdict": alert.verdict,
        "action": alert.action,
        "evidence": fs.get("evidence"),
        "links": links,
        "link_count": len(links),
        "dismissed_reason": meta.get("dismissed_reason"),
        "quality": bool(meta.get("quality")),
    }


def list_alerts() -> tuple[list[dict], bool]:
    """Every alert in the store, newest update first, plus whether the PR-state
    hydration of link chips is still pending behind the cold PR-snapshot load."""
    _sync_store_root()
    pr_states, pr_states_loading = _store_pr_states()
    rows = [_row(a, pr_states) for a in alert_data.alerts().values()]
    rows.sort(key=lambda r: r["updated_at"] or "", reverse=True)
    return rows, pr_states_loading


def get_alert(source: str, number: int) -> dict | None:
    """One alert's detail row plus its full meta (locations, advisory ids,
    dismissal bookkeeping) and the valid dismissal reasons for its source."""
    _sync_store_root()
    try:
        i = alert_id(source, int(number))
    except KeyError:
        return None
    alert = alert_data.alerts().get(i)
    if alert is None:
        return None
    row = _row(alert, _store_pr_states()[0])
    meta = alert.section("meta") or {}
    row["meta"] = {k: v for k, v in meta.items()
                   if k not in ("checked_at",)}
    row["dismiss_reasons"] = sorted(DISMISS_REASONS.get(source, set()))
    return row


_SEVERITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}
_SORT_KEYS = {
    "number": lambda r: r["number"],
    "source": lambda r: r["source"],
    "state": lambda r: r["state"],
    "severity": lambda r: _SEVERITY_RANK.get(r["severity"] or "", -1),
    "verdict": lambda r: r["verdict"] or "",
    "title": lambda r: (r["title"] or "").lower(),
    "links": lambda r: r["link_count"],
    "updated": lambda r: r["updated_at"] or "",
}
_DEFAULT_DESC = {"severity", "updated", "links", "number"}


def query_alerts(q: str = "", sort: str | None = None, direction: str | None = None,
                 source: str | None = None, state: str | None = None,
                 severity: str | list[str] | None = None,
                 verdict: str | None = None,
                 offset: int = 0, limit: int = 50) -> dict:
    """Paginated Alerts-table query. `source`/`state` are exact matches ("all"
    or None returns everything); `severity` accepts one value or a list
    (OR'd); `verdict` filters the fix-scan verdict, with "none" selecting
    unscanned alerts; `q` is a case-insensitive substring match over number,
    title, rule id, package, secret type, and path."""
    rows, pr_states_loading = list_alerts()
    if source and source != "all":
        rows = [r for r in rows if r["source"] == source]
    if state and state != "all":
        rows = [r for r in rows if r["state"] == state]
    if severity:
        wanted = severity if isinstance(severity, list) else [severity]
        rows = [r for r in rows if r["severity"] in wanted]
    if verdict:
        rows = [r for r in rows if (r["verdict"] or "none") == verdict]
    needle = q.strip().lower()
    if needle:
        rows = [r for r in rows
                if needle == str(r["number"])
                or any(needle in (r[k] or "").lower()
                       for k in ("title", "rule_id", "package", "secret_type", "path"))]
    key = _SORT_KEYS.get(sort or "", _SORT_KEYS["updated"])
    reverse = (direction == "desc" if direction in ("asc", "desc")
               else (sort or "updated") in _DEFAULT_DESC)
    rows.sort(key=lambda r: (key(r), r["id"]), reverse=reverse)
    total = len(rows)
    return {"items": rows[offset:offset + limit], "total": total,
            "offset": offset, "limit": limit,
            "pr_states_loading": pr_states_loading}


_sources_cache: dict[str, bool] | None = None


def sources_available() -> dict[str, bool]:
    """Which alert sources and the advisory feed this deployment can read,
    probed once per process with a 1-item list as the bot. No mintable token
    ⇒ all False."""
    global _sources_cache
    if _sources_cache is None:
        from prospector_app.backend import executor
        token = executor.mint_bot_token()
        probed: dict[str, bool] = {}
        paths = {
            "code-scanning": f"repos/{alert_config.REPO}/code-scanning/alerts",
            "dependabot": f"repos/{alert_config.REPO}/dependabot/alerts",
            "secret-scanning": f"repos/{alert_config.REPO}/secret-scanning/alerts",
            "advisory": f"repos/{alert_config.REPO}/security-advisories",
        }
        for source, path in paths.items():
            if token is None:
                probed[source] = False
                continue
            try:
                alert_config.gh_alert_read(path, token, {"per_page": "1"}, source=source)
                probed[source] = True
            except Exception:
                probed[source] = False
        _sources_cache = probed
    return _sources_cache


def refresh_sources() -> None:
    global _sources_cache
    _sources_cache = None
