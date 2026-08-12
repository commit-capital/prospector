"""ingest_records: upsert-on-change, link recompute for open alerts only, and
fact preservation across meta rewrites."""
from alert_triage import alert_ingest
from alert_triage.alert_store import AlertStore, alert_id


def _meta(source: str = "dependabot", number: int = 1, **over) -> dict:
    meta = {
        "source": source, "number": number, "state": "open", "raw_state": "open",
        "severity": "high", "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-02T00:00:00Z",
        "html_url": f"https://github.com/o/r/security/{source}/{number}",
        "package": "lodash", "manifest_path": "web/package.json",
    }
    meta.update(over)
    return meta


PRS = [{"number": 10, "title": "Bump lodash from 1 to 2", "body": "",
        "state": "merged", "head_sha": "aaa"}]


def test_new_alert_lands_with_meta_and_links(tmp_path):
    store = AlertStore(tmp_path)
    written = alert_ingest.ingest_records(store, [_meta()], PRS, {})
    assert written == 1
    a = store.load_alert(alert_id("dependabot", 1))
    assert a is not None and a.state == "open"
    assert [(c["number"], c["how"]) for c in a.candidates] == [(10, "manifest-bump")]


def test_unchanged_reingest_writes_nothing(tmp_path):
    store = AlertStore(tmp_path)
    alert_ingest.ingest_records(store, [_meta()], PRS, {})
    assert alert_ingest.ingest_records(store, [_meta()], PRS, {}) == 0


def test_changed_updated_at_rewrites_meta_and_links(tmp_path):
    store = AlertStore(tmp_path)
    alert_ingest.ingest_records(store, [_meta()], PRS, {})
    moved = _meta(updated_at="2026-08-09T00:00:00Z")
    assert alert_ingest.ingest_records(store, [moved], PRS, {}) == 1
    a = store.load_alert(alert_id("dependabot", 1))
    assert a is not None and a.updated_at == "2026-08-09T00:00:00Z"


def test_closed_alert_gets_no_links(tmp_path):
    store = AlertStore(tmp_path)
    alert_ingest.ingest_records(store, [_meta(state="fixed", raw_state="fixed")], PRS, {})
    a = store.load_alert(alert_id("dependabot", 1))
    assert a is not None and a.state == "fixed" and a.candidates == []


def test_fix_scan_survives_meta_rewrite(tmp_path):
    store = AlertStore(tmp_path)
    alert_ingest.ingest_records(store, [_meta()], PRS, {})
    store.edit_alert(alert_id("dependabot", 1)).record_fix_scan(
        "not-fixed", action="needs-fix", by="agent")
    alert_ingest.ingest_records(store, [_meta(updated_at="2026-08-10T00:00:00Z")], PRS, {})
    a = store.load_alert(alert_id("dependabot", 1))
    assert a is not None and a.verdict == "not-fixed"
