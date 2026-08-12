"""Alert domain wrapper + freshness: stamped mutators, staleness on updated_at
moves, the fix-scan max-age window, and link merging."""
from alert_triage.alert_freshness import FIX_SCAN_MAX_AGE_DAYS, is_current
from alert_triage.alert_model import Alert
from alert_triage.alert_store import AlertStore, alert_id


def _meta(source: str = "code-scanning", number: int = 5, **over) -> dict:
    meta = {
        "source": source, "number": number, "state": "open", "raw_state": "open",
        "severity": "high", "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-02T00:00:00Z",
        "html_url": f"https://github.com/o/r/security/{source}/{number}",
    }
    meta.update(over)
    return meta


def _alert(store: AlertStore, source: str = "code-scanning", number: int = 5,
           **meta_over) -> Alert:
    a = Alert(store, {"id": alert_id(source, number)})
    a.apply_facts(_meta(source, number, **meta_over))
    return a


def test_fix_scan_current_then_stale_on_updated_at_move(tmp_path):
    store = AlertStore(tmp_path)
    a = _alert(store)
    assert not is_current(a, "fix_scan")
    a.record_fix_scan("not-fixed", action="needs-fix", evidence="still present", by="agent")
    assert is_current(a, "fix_scan")
    a.set_meta(_meta(updated_at="2026-08-09T00:00:00Z"))
    assert not is_current(a, "fix_scan")


def test_fix_scan_max_age_window(tmp_path):
    store = AlertStore(tmp_path)
    a = _alert(store)
    a.record_fix_scan("not-fixed", by="agent")
    checked = a.rec["fix_scan"]["checked_at"][:10]
    assert is_current(a, "fix_scan", max_age_days=FIX_SCAN_MAX_AGE_DAYS, today=checked)
    assert not is_current(a, "fix_scan", max_age_days=7, today="2027-01-01")


def test_record_fix_scan_merges_links_deduped(tmp_path):
    store = AlertStore(tmp_path)
    a = _alert(store, "dependabot", 4)
    a.record_links([{"kind": "pr", "number": 10, "how": "manifest-bump"}])
    a.record_fix_scan("likely-fixed", action="dismiss-fixed", by="agent",
                      links=[{"kind": "pr", "number": 10, "how": "agent"},
                             {"kind": "issue", "number": 33, "how": "agent"}])
    cands = a.candidates
    assert [(c["kind"], c["number"]) for c in cands] == [("pr", 10), ("issue", 33)]
    assert cands[0]["how"] == "manifest-bump"
    assert a.verdict == "likely-fixed" and a.action == "dismiss-fixed"


def test_record_live_state_persists(tmp_path):
    store = AlertStore(tmp_path)
    _alert(store, "secret-scanning", 9)
    a = store.edit_alert(alert_id("secret-scanning", 9))
    a.record_live_state("dismissed", raw_state="resolved")
    again = store.load_alert(alert_id("secret-scanning", 9))
    assert again is not None
    assert again.state == "dismissed" and again.raw_state == "resolved"


def test_title_by_source(tmp_path):
    store = AlertStore(tmp_path)
    cs = _alert(store, "code-scanning", 1, rule_id="js/xss",
                rule_description="Reflected XSS")
    dep = _alert(store, "dependabot", 1, package="lodash", summary="Prototype pollution")
    sec = _alert(store, "secret-scanning", 1, secret_type="github_pat",
                 secret_type_display_name="GitHub Personal Access Token")
    assert cs.title == "Reflected XSS"
    assert dep.title == "Prototype pollution"
    assert sec.title == "GitHub Personal Access Token"
