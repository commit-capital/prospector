"""Fixed-pass driver: candidate selection/ordering, tier-0 deterministic
verdicts, bundle shape, and verdict application."""
import pytest

from alert_triage import alert_fixed_driver
from alert_triage.alert_store import AlertStore, alert_id


def _meta(source: str, number: int, **over) -> dict:
    meta = {
        "source": source, "number": number, "state": "open", "raw_state": "open",
        "severity": "medium", "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-02T00:00:00Z",
        "html_url": f"https://github.com/o/r/security/{source}/{number}",
    }
    meta.update(over)
    return meta


def _seed(store: AlertStore, source: str, number: int, **over):
    from alert_triage.alert_model import Alert
    a = Alert(store, {"id": alert_id(source, number)})
    a.apply_facts(_meta(source, number, **over))
    return a


def test_candidates_open_unscanned_severity_ordered(tmp_path):
    store = AlertStore(tmp_path)
    _seed(store, "code-scanning", 1, severity="low")
    _seed(store, "dependabot", 2, severity="critical")
    _seed(store, "secret-scanning", 3, severity="critical", state="fixed",
          raw_state="resolved")
    scanned = _seed(store, "code-scanning", 4, severity="high")
    scanned.record_fix_scan("not-fixed", by="agent")
    assert alert_fixed_driver.candidates(store) == [
        alert_id("dependabot", 2), alert_id("code-scanning", 1)]


def test_tier0_dependabot_merged_bump_at_fixed_version(tmp_path):
    store = AlertStore(tmp_path)
    a = _seed(store, "dependabot", 5, package="lodash", fixed_version="4.17.30")
    a.record_links([{"kind": "pr", "number": 10, "how": "manifest-bump",
                     "state": "merged", "bump_version": "4.17.30"}])
    out = alert_fixed_driver.deterministic_fixed(store)
    assert len(out) == 1
    v = out[0]
    assert v["id"] == alert_id("dependabot", 5)
    assert v["verdict"] == "likely-fixed" and v["action"] == "dismiss-fixed"
    assert "10" in v["evidence"]


def test_tier0_dependabot_open_bump_is_no_verdict(tmp_path):
    store = AlertStore(tmp_path)
    a = _seed(store, "dependabot", 6, package="x", fixed_version="2.0.0")
    a.record_links([{"kind": "pr", "number": 11, "how": "manifest-bump",
                     "state": "open", "bump_version": "2.0.0"}])
    assert alert_fixed_driver.deterministic_fixed(store) == []


def test_tier0_code_scanning_deleted_path(tmp_path):
    store = AlertStore(tmp_path)
    _seed(store, "code-scanning", 7, path="gone/file.ts")
    out = alert_fixed_driver.deterministic_fixed(store, path_exists=lambda p: False)
    assert [v["verdict"] for v in out] == ["likely-fixed"]
    out2 = alert_fixed_driver.deterministic_fixed(store, path_exists=lambda p: True)
    assert out2 == []


def test_tier0_without_path_probe_skips_rule(tmp_path):
    store = AlertStore(tmp_path)
    _seed(store, "code-scanning", 8, path="maybe/file.ts")
    assert alert_fixed_driver.deterministic_fixed(store, path_exists=None) == []


def test_bundle_shape(tmp_path):
    store = AlertStore(tmp_path)
    _seed(store, "dependabot", 9, package="lodash", ghsa_id="GHSA-1",
          fixed_version="2.0")
    entries = alert_fixed_driver.bundle(store)
    assert len(entries) == 1
    e = entries[0]
    assert e["id"] == alert_id("dependabot", 9)
    assert e["source"] == "dependabot" and e["number"] == 9
    assert e["candidates"] == []


def test_apply_verdicts_writes_and_ledgers(tmp_path):
    store = AlertStore(tmp_path)
    _seed(store, "code-scanning", 10)
    n = alert_fixed_driver.apply_verdicts(store, [
        {"id": alert_id("code-scanning", 10), "verdict": "fixed",
         "action": "dismiss-fixed", "evidence": "PR #12 removed the sink",
         "links": [{"kind": "pr", "number": 12, "how": "agent"}]}])
    assert n == 1
    a = store.load_alert(alert_id("code-scanning", 10))
    assert a is not None and a.verdict == "fixed" and a.action == "dismiss-fixed"
    assert [(c["kind"], c["number"]) for c in a.candidates] == [("pr", 12)]
    assert [r.phase for r in store.runs()] == ["alert-find-fixed"]


def test_apply_verdicts_rejects_bad_input(tmp_path):
    store = AlertStore(tmp_path)
    _seed(store, "code-scanning", 11)
    with pytest.raises(ValueError, match="verdict"):
        alert_fixed_driver.apply_verdicts(store, [
            {"id": alert_id("code-scanning", 11), "verdict": "who-knows"}])
    with pytest.raises(KeyError):
        alert_fixed_driver.apply_verdicts(store, [
            {"id": alert_id("code-scanning", 99), "verdict": "fixed"}])
