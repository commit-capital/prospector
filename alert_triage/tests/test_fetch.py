"""Per-source normalizers: state/severity mapping, identity fields, and the
hard guarantee that secret values never survive normalization."""
import json

from alert_triage import fetch_alerts
from alert_triage.alert_store import validate_alert, alert_id


CODE_SCANNING_RAW = {
    "number": 42,
    "created_at": "2026-07-01T10:00:00Z",
    "updated_at": "2026-07-20T10:00:00Z",
    "html_url": "https://github.com/o/r/security/code-scanning/42",
    "state": "open",
    "fixed_at": None,
    "dismissed_by": None,
    "dismissed_at": None,
    "dismissed_reason": None,
    "dismissed_comment": None,
    "rule": {
        "id": "js/reflected-xss",
        "name": "js/reflected-xss",
        "severity": "error",
        "security_severity_level": "high",
        "description": "Reflected cross-site scripting",
        "tags": ["security", "external/cwe/cwe-079"],
    },
    "tool": {"name": "CodeQL", "version": "2.20.0"},
    "most_recent_instance": {
        "ref": "refs/heads/trunk",
        "state": "open",
        "commit_sha": "deadbeef",
        "message": {"text": "User-provided value flows into HTML."},
        "location": {"path": "src/server/render.ts", "start_line": 88,
                     "end_line": 90, "start_column": 5, "end_column": 30},
    },
}

DEPENDABOT_RAW = {
    "number": 7,
    "state": "auto_dismissed",
    "created_at": "2026-06-01T00:00:00Z",
    "updated_at": "2026-06-15T00:00:00Z",
    "html_url": "https://github.com/o/r/security/dependabot/7",
    "dependency": {
        "package": {"ecosystem": "npm", "name": "lodash"},
        "manifest_path": "web/package.json",
        "scope": "runtime",
    },
    "security_advisory": {
        "ghsa_id": "GHSA-xxxx-yyyy-zzzz",
        "cve_id": "CVE-2026-1234",
        "summary": "Prototype pollution in lodash",
        "severity": "medium",
    },
    "security_vulnerability": {
        "package": {"ecosystem": "npm", "name": "lodash"},
        "severity": "medium",
        "vulnerable_version_range": "< 4.17.30",
        "first_patched_version": {"identifier": "4.17.30"},
    },
    "dismissed_at": "2026-06-15T00:00:00Z",
    "dismissed_reason": None,
    "dismissed_comment": None,
    "fixed_at": None,
    "auto_dismissed_at": "2026-06-15T00:00:00Z",
}

SECRET_RAW = {
    "number": 3,
    "created_at": "2026-05-01T00:00:00Z",
    "updated_at": "2026-05-09T00:00:00Z",
    "html_url": "https://github.com/o/r/security/secret-scanning/3",
    "state": "resolved",
    "resolution": "revoked",
    "resolution_comment": "rotated",
    "secret_type": "github_personal_access_token",
    "secret_type_display_name": "GitHub Personal Access Token",
    "secret": "ghp_SUPERSECRETVALUE123",
    "push_protection_bypassed": False,
}


def test_normalize_code_scanning():
    meta = fetch_alerts.normalize_code_scanning(CODE_SCANNING_RAW)
    assert meta["source"] == "code-scanning" and meta["number"] == 42
    assert meta["state"] == "open" and meta["raw_state"] == "open"
    assert meta["severity"] == "high"
    assert meta["rule_id"] == "js/reflected-xss"
    assert meta["tool"] == "CodeQL"
    assert meta["path"] == "src/server/render.ts" and meta["start_line"] == 88
    assert meta["quality"] is False
    validate_alert({"id": alert_id("code-scanning", 42), "meta": meta})


def test_code_scanning_quality_alert_severity_fallback():
    raw = json.loads(json.dumps(CODE_SCANNING_RAW))
    raw["rule"]["security_severity_level"] = None
    raw["rule"]["severity"] = "warning"
    meta = fetch_alerts.normalize_code_scanning(raw)
    assert meta["severity"] == "medium" and meta["quality"] is True


def test_normalize_dependabot_auto_dismissed_maps_to_dismissed():
    meta = fetch_alerts.normalize_dependabot(DEPENDABOT_RAW)
    assert meta["source"] == "dependabot" and meta["number"] == 7
    assert meta["state"] == "dismissed" and meta["raw_state"] == "auto_dismissed"
    assert meta["severity"] == "medium"
    assert meta["package"] == "lodash" and meta["ecosystem"] == "npm"
    assert meta["manifest_path"] == "web/package.json"
    assert meta["ghsa_id"] == "GHSA-xxxx-yyyy-zzzz"
    assert meta["fixed_version"] == "4.17.30"
    validate_alert({"id": alert_id("dependabot", 7), "meta": meta})


def test_normalize_secret_scanning_drops_secret_value():
    meta = fetch_alerts.normalize_secret_scanning(SECRET_RAW, locations=[
        {"path": "config/deploy.yml", "start_line": 4, "end_line": 4}])
    assert meta["source"] == "secret-scanning" and meta["number"] == 3
    assert meta["state"] == "fixed" and meta["raw_state"] == "resolved"
    assert meta["severity"] == "critical"
    assert meta["secret_type"] == "github_personal_access_token"
    assert meta["locations"] == [{"path": "config/deploy.yml", "start_line": 4,
                                  "end_line": 4}]
    assert "secret" not in meta
    assert "ghp_SUPERSECRETVALUE123" not in json.dumps(meta)
    validate_alert({"id": alert_id("secret-scanning", 3), "meta": meta})


def test_secret_scanning_non_revoked_resolution_maps_to_dismissed():
    raw = dict(SECRET_RAW, resolution="false_positive")
    meta = fetch_alerts.normalize_secret_scanning(raw, locations=[])
    assert meta["state"] == "dismissed" and meta["raw_state"] == "resolved"


def test_secret_scanning_open_stays_open():
    raw = dict(SECRET_RAW, state="open", resolution=None)
    meta = fetch_alerts.normalize_secret_scanning(raw, locations=[])
    assert meta["state"] == "open"


def test_paged_uses_gh_link_pagination_and_flattens(monkeypatch):
    """The pager must delegate pagination to gh (--paginate) rather than send a
    page number — Dependabot's endpoint paginates by cursor and rejects
    `page` — and flatten the --slurp array-of-pages into one row list."""
    import subprocess

    seen: dict = {}

    def fake_run(cmd, capture_output, text, env):
        seen["cmd"] = cmd

        class R:
            returncode = 0
            stdout = json.dumps([[{"n": i} for i in range(100)], [{"n": 100}]])
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    rows = fetch_alerts._paged("repos/o/r/dependabot/alerts", "tok", "dependabot")
    assert len(rows) == 101 and rows[-1] == {"n": 100}
    assert "--paginate" in seen["cmd"] and "--slurp" in seen["cmd"]
    assert not any(a.startswith("page=") for a in seen["cmd"])
    assert "per_page=100" in seen["cmd"]
