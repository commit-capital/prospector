"""dismiss_eligibility: the ONE upstream-dismissal policy, fail-closed."""
from alert_triage import alert_gates
from alert_triage.alert_model import Alert
from alert_triage.alert_store import AlertStore, alert_id


def _alert(store: AlertStore, source: str = "code-scanning", number: int = 1,
           state: str = "open") -> Alert:
    a = Alert(store, {"id": alert_id(source, number)})
    a.apply_facts({
        "source": source, "number": number, "state": state, "raw_state": state,
        "severity": "high", "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-02T00:00:00Z",
        "html_url": f"https://github.com/o/r/security/{source}/{number}",
    })
    return a


def test_closed_alert_refused(tmp_path):
    a = _alert(AlertStore(tmp_path), state="dismissed")
    ok, why = alert_gates.dismiss_eligibility(a, "won't fix", "note")
    assert not ok and "open" in why


def test_unknown_reason_refused(tmp_path):
    a = _alert(AlertStore(tmp_path))
    ok, why = alert_gates.dismiss_eligibility(a, "fix_started", "note")
    assert not ok and "reason" in why.lower()


def test_judgment_reason_requires_note(tmp_path):
    a = _alert(AlertStore(tmp_path))
    ok, why = alert_gates.dismiss_eligibility(a, "won't fix", "")
    assert not ok and "note" in why.lower()
    ok, _ = alert_gates.dismiss_eligibility(a, "won't fix", "accepted risk, tracked in #9")
    assert ok


def test_fixed_evidence_reason_requires_current_verdict(tmp_path):
    store = AlertStore(tmp_path)
    a = _alert(store, "dependabot", 2)
    ok, why = alert_gates.dismiss_eligibility(a, "fix_started", "")
    assert not ok and "verdict" in why.lower()
    a.record_fix_scan("not-fixed", by="agent")
    ok, why = alert_gates.dismiss_eligibility(a, "fix_started", "")
    assert not ok
    a.record_fix_scan("likely-fixed", action="dismiss-fixed", by="agent")
    ok, why = alert_gates.dismiss_eligibility(a, "fix_started", "")
    assert ok, why


def test_fixed_evidence_reason_stale_verdict_refused(tmp_path):
    store = AlertStore(tmp_path)
    a = _alert(store, "secret-scanning", 3)
    a.record_fix_scan("fixed", by="agent")
    ok, _ = alert_gates.dismiss_eligibility(a, "revoked", "")
    assert ok
    ok, why = alert_gates.dismiss_eligibility(a, "revoked", "", today="2027-06-01")
    assert not ok and "verdict" in why.lower()


def test_per_source_reason_vocabulary():
    assert alert_gates.DISMISS_REASONS["code-scanning"] == {
        "false positive", "won't fix", "used in tests"}
    assert "fix_started" in alert_gates.DISMISS_REASONS["dependabot"]
    assert "revoked" in alert_gates.DISMISS_REASONS["secret-scanning"]
