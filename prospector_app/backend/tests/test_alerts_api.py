"""Alerts backend: row projection, query filters/sorts, detail, and the gated
executor dismissal (dry-run forcing, activity logging, store write-back)."""
import pytest

from alert_triage.alert_model import Alert
from alert_triage.alert_store import AlertStore, alert_id
from prospector_app.backend import alert_data
from prospector_app.backend import alerts as alerts_mod
from prospector_app.backend import executor


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A tmp alert store with one alert per source, wired into the app's alert
    snapshot; PR-state hydration stubbed off the real PR snapshot."""
    store = AlertStore(tmp_path)

    def seed(source: str, number: int, **over):
        meta = {
            "source": source, "number": number, "state": "open",
            "raw_state": "open", "severity": "medium",
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": f"2026-08-0{number}T00:00:00Z",
            "html_url": f"https://github.com/o/r/security/{source}/{number}",
        }
        meta.update(over)
        a = Alert(store, {"id": alert_id(source, number)})
        a.apply_facts(meta)
        return a

    cs = seed("code-scanning", 1, severity="high", rule_id="js/xss",
              rule_description="Reflected XSS", path="src/render.ts", start_line=8)
    dep = seed("dependabot", 2, severity="critical", package="lodash",
               summary="Prototype pollution")
    dep.record_links([{"kind": "pr", "number": 10, "how": "manifest-bump",
                       "state": "open"}])
    dep.record_fix_scan("likely-fixed", action="dismiss-fixed",
                        evidence="merged bump", by="deterministic")
    seed("secret-scanning", 3, secret_type="github_pat",
         secret_type_display_name="GitHub PAT", severity="critical")
    monkeypatch.setattr(alerts_mod, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(alerts_mod, "_synced_store_root", None)
    monkeypatch.setattr(alerts_mod, "_store_pr_states", lambda: {10: "merged"})
    yield store, cs, dep
    alerts_mod.STORE_ROOT = None
    alerts_mod._synced_store_root = None
    alert_data.set_store_root(None)


def test_list_alerts_rows(seeded):
    rows = alerts_mod.list_alerts()
    assert [r["number"] for r in rows] == [3, 2, 1]  # newest updated first
    dep = next(r for r in rows if r["source"] == "dependabot")
    assert dep["title"] == "Prototype pollution"
    assert dep["verdict"] == "likely-fixed" and dep["action"] == "dismiss-fixed"
    assert dep["links"][0]["state"] == "merged"  # hydrated from PR store


def test_query_filters_and_sorts(seeded):
    out = alerts_mod.query_alerts(source="dependabot")
    assert [r["number"] for r in out["items"]] == [2] and out["total"] == 1
    out = alerts_mod.query_alerts(verdict="none")
    assert sorted(r["number"] for r in out["items"]) == [1, 3]
    out = alerts_mod.query_alerts(sort="severity")
    assert [r["severity"] for r in out["items"]] == ["critical", "critical", "high"]
    out = alerts_mod.query_alerts(q="lodash")
    assert [r["number"] for r in out["items"]] == [2]
    out = alerts_mod.query_alerts(severity=["high"])
    assert [r["number"] for r in out["items"]] == [1]


def test_get_alert_detail(seeded):
    d = alerts_mod.get_alert("code-scanning", 1)
    assert d is not None
    assert d["rule_id"] == "js/xss" and d["meta"]["path"] == "src/render.ts"
    assert d["dismiss_reasons"] == ["false positive", "used in tests", "won't fix"]
    assert alerts_mod.get_alert("code-scanning", 99) is None
    assert alerts_mod.get_alert("nessus", 1) is None


@pytest.fixture
def logged(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(
        "prospector_app.backend.activity.record",
        lambda kind, **f: events.append({"kind": kind, **f}) or {"kind": kind, **f})
    return events


def test_dismiss_gate_blocked_never_runs_gh(seeded, logged, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr("prospector_app.backend.safety_guard.alert_bot_run",
                        lambda argv, token: calls.append(argv))
    res = executor.dismiss_alert("code-scanning", 1, "won't fix", "",
                                 token="tok", dry_run=False)
    assert res["status"] == "blocked" and "note" in res["detail"]
    assert calls == []
    assert logged[-1]["kind"] == "alert-dismiss"


def test_dismiss_dry_run_and_forced_dry_run(seeded, logged):
    res = executor.dismiss_alert("code-scanning", 1, "won't fix", "accepted risk",
                                 token="tok", dry_run=True)
    assert res["status"] == "dry-run"
    assert logged[-1]["dry_run"] is True
    res = executor.dismiss_alert("code-scanning", 1, "won't fix", "accepted risk",
                                 token=None, dry_run=False)
    assert res["status"] == "dry-run" and res["forced"] is True


def test_dismiss_live_success_persists_state(seeded, logged, monkeypatch):
    store, _, _ = seeded

    class Ok:
        returncode = 0
        stderr = ""

    seen: dict = {}

    def fake_run(argv, token):
        seen["argv"] = argv
        seen["token"] = token
        return Ok()

    monkeypatch.setattr("prospector_app.backend.executor.alert_bot_run", fake_run,
                        raising=False)
    monkeypatch.setattr("prospector_app.backend.safety_guard.alert_bot_run", fake_run)
    res = executor.dismiss_alert("dependabot", 2, "fix_started", "",
                                 token="tok", dry_run=False)
    assert res["status"] == "executed", res["detail"]
    joined = " ".join(seen["argv"])
    assert "repos/test-owner/test-repo/dependabot/alerts/2" in joined
    assert "dismissed_reason=fix_started" in joined
    a = store.load_alert(alert_id("dependabot", 2))
    assert a is not None and a.state == "dismissed"
    assert logged[-1]["dry_run"] is False and logged[-1]["kind"] == "alert-dismiss"


def test_dismiss_live_failure_reports_error(seeded, logged, monkeypatch):
    class Fail:
        returncode = 1
        stderr = "HTTP 422 boom"

    monkeypatch.setattr("prospector_app.backend.safety_guard.alert_bot_run",
                        lambda argv, token: Fail())
    res = executor.dismiss_alert("dependabot", 2, "fix_started", "",
                                 token="tok", dry_run=False)
    assert res["status"] == "error" and "422" in res["detail"]
    store, _, dep = seeded
    a = store.load_alert(alert_id("dependabot", 2))
    assert a is not None and a.state == "open"
