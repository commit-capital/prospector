"""AlertStore: synthetic ids, validation, roundtrip, watermarks, runs ledger."""
import pytest

from alert_triage import alert_store
from alert_triage.alert_store import AlertStore, alert_id, validate_alert
from pipeline.storekit import ValidationError, now


def _meta(source: str = "code-scanning", number: int = 5, **over) -> dict:
    meta = {
        "source": source, "number": number, "state": "open", "raw_state": "open",
        "severity": "high", "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-02T00:00:00Z",
        "html_url": f"https://github.com/o/r/security/{source}/{number}",
    }
    meta.update(over)
    return meta


def _rec(source: str = "code-scanning", number: int = 5, **meta_over) -> dict:
    return {"id": alert_id(source, number), "meta": _meta(source, number, **meta_over)}


def test_alert_id_is_stable_and_disjoint_across_sources():
    assert alert_id("code-scanning", 5) == 10_000_005
    assert alert_id("dependabot", 5) == 20_000_005
    assert alert_id("secret-scanning", 5) == 30_000_005
    assert alert_id("code-scanning", 5) == alert_id("code-scanning", 5)


def test_validate_accepts_minimal_record():
    validate_alert(_rec())


def test_validate_rejects_missing_meta():
    with pytest.raises(ValidationError, match="meta"):
        validate_alert({"id": alert_id("dependabot", 1)})


def test_validate_rejects_bad_source_state_severity():
    bad_source = _rec()
    bad_source["meta"]["source"] = "nessus"
    with pytest.raises(ValidationError, match="source"):
        validate_alert(bad_source)
    bad_state = _rec()
    bad_state["meta"]["state"] = "resolved"
    with pytest.raises(ValidationError, match="state"):
        validate_alert(bad_state)
    bad_sev = _rec()
    bad_sev["meta"]["severity"] = "warning"
    with pytest.raises(ValidationError, match="severity"):
        validate_alert(bad_sev)


def test_validate_rejects_id_source_number_mismatch():
    rec = _rec("dependabot", 7)
    rec["id"] = alert_id("dependabot", 8)
    with pytest.raises(ValidationError, match="id"):
        validate_alert(rec)


def test_validate_rejects_stored_secret_value():
    rec = _rec("secret-scanning", 2)
    rec["meta"]["secret"] = "hunter2"
    with pytest.raises(ValidationError, match="secret"):
        validate_alert(rec)


def test_validate_rejects_bad_fix_scan_and_links():
    rec = _rec()
    rec["fix_scan"] = {"verdict": "maybe"}
    with pytest.raises(ValidationError, match="verdict"):
        validate_alert(rec)
    rec = _rec()
    rec["fix_scan"] = {"verdict": "fixed", "action": "yolo"}
    with pytest.raises(ValidationError, match="action"):
        validate_alert(rec)
    rec = _rec()
    rec["links"] = {"candidates": "nope"}
    with pytest.raises(ValidationError, match="candidates"):
        validate_alert(rec)


def test_roundtrip_and_since_watermark(tmp_path):
    store = AlertStore(tmp_path)
    store.save_alert(_rec("dependabot", 1))
    store.save_alert(_rec("secret-scanning", 1))
    assert sorted(store.all_alerts()) == [alert_id("dependabot", 1),
                                          alert_id("secret-scanning", 1)]
    everything, hi = store.alerts_since(None)
    assert len(everything) == 2 and hi is not None
    delta, hi2 = store.alerts_since(hi)
    assert delta == {} and (hi2 is None or hi2 <= hi)
    store.save_alert(_rec("dependabot", 1, state="dismissed"))
    delta, _ = store.alerts_since(hi)
    assert list(delta) == [alert_id("dependabot", 1)]
    assert delta[alert_id("dependabot", 1)].state == "dismissed"


def test_edit_alert_loads_bound_view(tmp_path):
    store = AlertStore(tmp_path)
    store.save_alert(_rec("code-scanning", 3))
    a = store.edit_alert(alert_id("code-scanning", 3))
    assert a.source == "code-scanning" and a.number == 3 and a.state == "open"
    with pytest.raises(KeyError):
        store.edit_alert(alert_id("code-scanning", 99))


def test_runs_ledger_kind_alert(tmp_path):
    store = AlertStore(tmp_path)
    store.append_run({"phase": "alert-ingest", "started": now(), "finished": now(),
                      "stats": {"upserted": 2}})
    runs = store.runs()
    assert len(runs) == 1 and runs[0].phase == "alert-ingest"


def test_vocabularies():
    assert alert_store.ALERT_SOURCES == {"code-scanning", "dependabot", "secret-scanning"}
    assert alert_store.ALERT_STATES == {"open", "dismissed", "fixed"}
    assert alert_store.FIX_SCAN_STATES == {"fixed", "likely-fixed", "not-fixed"}
    assert alert_store.ALERT_ACTIONS == {"dismiss-fixed", "needs-fix", "needs-human"}
