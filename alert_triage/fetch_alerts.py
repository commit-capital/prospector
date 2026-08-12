"""Read-only fetch of GitHub repository-security alerts, normalized per source.

Each normalizer maps one raw API payload onto the meta dict validate_alert
expects: normalized `state` (open | dismissed | fixed) with the raw state
preserved, normalized `severity` (critical | high | medium | low), and the
per-source identity fields. Secret-scanning payloads carry the secret value;
it is dropped here and never reaches the store.
"""
from __future__ import annotations

from alert_triage import config

# Rule severity → normalized severity, for code-scanning alerts without a
# security_severity_level (pure quality findings).
SEVERITY_FALLBACK = {"error": "high", "warning": "medium", "note": "low", "none": "low"}

_SEVERITIES = {"critical", "high", "medium", "low"}

# Trimmed location entries kept per secret-scanning alert.
_LOCATION_CAP = 10


def _severity(value: str | None, fallback: str = "low") -> str:
    v = (value or "").lower()
    return v if v in _SEVERITIES else fallback


def normalize_code_scanning(raw: dict) -> dict:
    rule = raw.get("rule") or {}
    inst = raw.get("most_recent_instance") or {}
    loc = inst.get("location") or {}
    security_severity = rule.get("security_severity_level")
    severity = (_severity(security_severity)
                if security_severity
                else SEVERITY_FALLBACK.get((rule.get("severity") or "").lower(), "low"))
    return {
        "source": "code-scanning",
        "number": raw["number"],
        "state": raw.get("state", "open"),
        "raw_state": raw.get("state", "open"),
        "severity": severity,
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "html_url": raw.get("html_url"),
        "rule_id": rule.get("id"),
        "rule_name": rule.get("name"),
        "rule_description": rule.get("description"),
        "security_severity": security_severity,
        "quality": security_severity is None,
        "tool": (raw.get("tool") or {}).get("name"),
        "ref": inst.get("ref"),
        "instance_message": ((inst.get("message") or {}).get("text") or "")[:500] or None,
        "path": loc.get("path"),
        "start_line": loc.get("start_line"),
        "end_line": loc.get("end_line"),
        "dismissed_reason": raw.get("dismissed_reason"),
        "dismissed_comment": raw.get("dismissed_comment"),
        "dismissed_at": raw.get("dismissed_at"),
        "fixed_at": raw.get("fixed_at"),
    }


def normalize_dependabot(raw: dict) -> dict:
    dep = raw.get("dependency") or {}
    pkg = dep.get("package") or {}
    adv = raw.get("security_advisory") or {}
    vuln = raw.get("security_vulnerability") or {}
    raw_state = raw.get("state", "open")
    state = "dismissed" if raw_state == "auto_dismissed" else raw_state
    first_patched = vuln.get("first_patched_version") or {}
    return {
        "source": "dependabot",
        "number": raw["number"],
        "state": state,
        "raw_state": raw_state,
        "severity": _severity(adv.get("severity")),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "html_url": raw.get("html_url"),
        "package": pkg.get("name"),
        "ecosystem": pkg.get("ecosystem"),
        "manifest_path": dep.get("manifest_path"),
        "dependency_scope": dep.get("scope"),
        "ghsa_id": adv.get("ghsa_id"),
        "cve_id": adv.get("cve_id"),
        "summary": adv.get("summary"),
        "vulnerable_range": vuln.get("vulnerable_version_range"),
        "fixed_version": first_patched.get("identifier"),
        "dismissed_reason": raw.get("dismissed_reason"),
        "dismissed_comment": raw.get("dismissed_comment"),
        "dismissed_at": raw.get("dismissed_at"),
        "fixed_at": raw.get("fixed_at"),
    }


def normalize_secret_scanning(raw: dict, locations: list[dict]) -> dict:
    """`locations` is the pre-fetched, trimmed location list (path + line span
    only). The payload's `secret` value is deliberately not read."""
    raw_state = raw.get("state", "open")
    resolution = raw.get("resolution")
    if raw_state == "resolved":
        state = "fixed" if resolution == "revoked" else "dismissed"
    else:
        state = "open"
    return {
        "source": "secret-scanning",
        "number": raw["number"],
        "state": state,
        "raw_state": raw_state,
        "severity": "critical",
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "html_url": raw.get("html_url"),
        "secret_type": raw.get("secret_type"),
        "secret_type_display_name": raw.get("secret_type_display_name"),
        "resolution": resolution,
        "resolution_comment": raw.get("resolution_comment"),
        "push_protection_bypassed": bool(raw.get("push_protection_bypassed")),
        "locations": locations[:_LOCATION_CAP],
        "dismissed_reason": resolution if state == "dismissed" else None,
    }


def _paged(path: str, token: str, source: str) -> list[dict]:
    """Fetch every page of an alert list endpoint (100/page). The 100-page
    backstop bounds a runaway loop at 10,000 alerts; exhausting it raises
    rather than returning a silently truncated corpus."""
    out: list[dict] = []
    for page in range(1, 101):
        rows = config.gh_alert_read(
            path, token, {"per_page": "100", "page": str(page)}, source=source)
        assert isinstance(rows, list)
        out += rows
        if len(rows) < 100:
            return out
    raise RuntimeError(f"{source}: fetch exceeded the 100-page backstop ({len(out)} alerts)")


def _secret_locations(number: int, token: str) -> list[dict]:
    """The trimmed commit locations for one secret-scanning alert: path + line
    span only, capped. Location fetch failures degrade to an empty list — the
    alert itself is still worth storing."""
    try:
        rows = config.gh_alert_read(
            f"repos/{config.REPO}/secret-scanning/alerts/{number}/locations",
            token, {"per_page": str(_LOCATION_CAP)}, source="secret-scanning")
    except Exception:
        return []
    out: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        det = row.get("details") or {}
        if row.get("type") == "commit" and det.get("path"):
            out.append({"path": det["path"], "start_line": det.get("start_line"),
                        "end_line": det.get("end_line")})
    return out[:_LOCATION_CAP]


def fetch_source(source: str, token: str) -> list[dict]:
    """Every alert of one source (all states), normalized. Raises
    SourceUnavailable when the repository/App lacks the feature or permission."""
    raws = _paged(f"repos/{config.REPO}/{source}/alerts", token, source)
    if source == "code-scanning":
        return [normalize_code_scanning(r) for r in raws]
    if source == "dependabot":
        return [normalize_dependabot(r) for r in raws]
    if source == "secret-scanning":
        return [normalize_secret_scanning(r, _secret_locations(r["number"], token))
                for r in raws]
    raise ValueError(f"unknown alert source {source!r}")
