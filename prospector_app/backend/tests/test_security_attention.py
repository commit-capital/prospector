"""The Home Security card's backend: which advisories and alerts count as
open security work, and how the sample items are ordered."""
import pytest

from alert_triage.advisory_model import Advisory
from alert_triage.advisory_store import AdvisoryStore, advisory_id
from alert_triage.alert_model import Alert
from alert_triage.alert_store import AlertStore, alert_id
from prospector_app.backend import advisories as adv_mod
from prospector_app.backend import advisory_data
from prospector_app.backend import alert_data
from prospector_app.backend import alerts as alerts_mod
from prospector_app.backend import security_attention

CRIT_OLD = "GHSA-2222-2222-2223"
CRIT_NEW = "GHSA-2222-2222-2224"
HIGH_UNSCANNED = "GHSA-2222-2222-2225"
HIGH_FIXED = "GHSA-2222-2222-2226"
MEDIUM_OPEN = "GHSA-2222-2222-2227"
CRIT_PUBLISHED = "GHSA-2222-2222-2228"


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    adv_store = AdvisoryStore(tmp_path)

    def seed_advisory(ghsa: str, **over) -> Advisory:
        meta = {
            "ghsa_id": ghsa, "state": "triage", "severity": "medium",
            "summary": f"report {ghsa}", "reporter": "alice",
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-01T00:00:00Z",
            "html_url": f"https://github.com/o/r/security/advisories/{ghsa}",
        }
        meta.update(over)
        a = Advisory(adv_store, {"id": advisory_id(ghsa)})
        a.apply_facts(meta)
        return a

    seed_advisory(CRIT_OLD, severity="critical", created_at="2026-07-01T00:00:00Z")
    crit_new = seed_advisory(CRIT_NEW, severity="critical",
                             created_at="2026-08-01T00:00:00Z")
    crit_new.record_fix_scan("not-fixed", by="agent", evidence="still present")
    seed_advisory(HIGH_UNSCANNED, severity="high", state="draft",
                  created_at="2026-06-01T00:00:00Z")
    fixed = seed_advisory(HIGH_FIXED, severity="high")
    fixed.record_fix_scan("fixed", by="agent", fix_commit="c647b8cc2ea6",
                          evidence="gone")
    seed_advisory(MEDIUM_OPEN)
    seed_advisory(CRIT_PUBLISHED, severity="critical", state="published")

    alert_store = AlertStore(tmp_path)

    def seed_alert(source: str, number: int, **over) -> Alert:
        meta = {
            "source": source, "number": number, "state": "open",
            "raw_state": "open", "severity": "medium",
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-01T00:00:00Z",
            "html_url": f"https://github.com/o/r/security/{source}/{number}",
        }
        meta.update(over)
        a = Alert(alert_store, {"id": alert_id(source, number)})
        a.apply_facts(meta)
        return a

    seed_alert("secret-scanning", 1, severity="critical", secret_type="github_pat",
               secret_type_display_name="GitHub PAT",
               created_at="2026-05-01T00:00:00Z")
    seed_alert("secret-scanning", 2, severity="critical", state="fixed",
               raw_state="resolved", secret_type="github_pat")
    seed_alert("code-scanning", 3, severity="critical", rule_id="js/xss")

    monkeypatch.setattr(adv_mod, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(adv_mod, "_synced_store_root", None)
    monkeypatch.setattr(adv_mod, "_store_pr_states", lambda: ({}, False))
    monkeypatch.setattr(alerts_mod, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(alerts_mod, "_synced_store_root", None)
    monkeypatch.setattr(alerts_mod, "_store_pr_states", lambda: ({}, False))
    yield
    adv_mod.STORE_ROOT = None
    adv_mod._synced_store_root = None
    advisory_data.set_store_root(None)
    alerts_mod.STORE_ROOT = None
    alerts_mod._synced_store_root = None
    alert_data.set_store_root(None)


def test_counts_severe_unfixed_advisories_and_open_secrets(seeded):
    out = security_attention.attention()
    # In: both open criticals (one unscanned, one not-fixed), the unscanned
    # high, and the one open secret alert. Out: the fixed high, the medium,
    # the published critical, the resolved secret, and the code-scanning alert.
    assert out["advisories"] == 3
    assert out["secrets"] == 1
    assert out["total"] == 4


def test_items_sort_most_severe_then_oldest(seeded):
    out = security_attention.attention()
    keys = [i["key"] for i in out["items"]]
    # Criticals before highs; within the criticals the secret alert (May) is
    # older than both advisories (July, August).
    assert keys == ["secret-1", CRIT_OLD, CRIT_NEW][:security_attention.SAMPLE_LIMIT]


def test_item_shapes_carry_the_detail_link_fields(seeded):
    out = security_attention.attention()
    secret = next(i for i in out["items"] if i["kind"] == "secret")
    assert secret["number"] == 1 and secret["severity"] == "critical"
    advisory = next(i for i in out["items"] if i["kind"] == "advisory")
    assert advisory["ghsa_id"] == CRIT_OLD
    assert advisory["html_url"].endswith(CRIT_OLD)


def test_route(seeded):
    from fastapi.testclient import TestClient
    from prospector_app.backend import app as appmod
    c = TestClient(appmod.app, raise_server_exceptions=False)
    r = c.get("/api/security/attention")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 4 and len(body["items"]) <= security_attention.SAMPLE_LIMIT
