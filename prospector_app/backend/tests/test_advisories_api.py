"""Advisories backend: row projection, query filters, detail, caps probe."""
import pytest

from alert_triage.advisory_model import Advisory
from alert_triage.advisory_store import AdvisoryStore, advisory_id
from prospector_app.backend import advisories as adv_mod
from prospector_app.backend import advisory_data

G1, G2, G3 = "GHSA-2222-2222-2223", "GHSA-2222-2222-2224", "GHSA-2222-2222-2225"


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    store = AdvisoryStore(tmp_path)

    def seed(ghsa: str, **over) -> Advisory:
        meta = {
            "ghsa_id": ghsa, "state": "triage", "severity": "medium",
            "summary": f"report {ghsa}", "description": "## Details\nbody",
            "cve_id": None, "cwe_ids": [], "reporter": "alice", "author": "alice",
            "created_at": "2026-08-01T00:00:00Z", "updated_at": "2026-08-01T00:00:00Z",
            "published_at": None, "closed_at": None,
            "html_url": f"https://github.com/o/r/security/advisories/{ghsa}",
            "vulnerable_range": None, "patched_versions": None,
        }
        meta.update(over)
        a = Advisory(store, {"id": advisory_id(ghsa)})
        a.apply_facts(meta)
        return a

    a = seed(G1, severity="critical", summary="SSRF via skill import",
             updated_at="2026-08-03T00:00:00Z")
    a.apply_facts(a.section("meta") or {},
                  links=[{"kind": "pr", "number": 10, "how": "text-ref", "state": "open"}])
    a.record_fix_scan("fixed", by="agent", fix_commit="c647b8cc2ea6", evidence="gone")
    b = seed(G2, summary="SSRF via skill import (again)", reporter="bob",
             updated_at="2026-08-02T00:00:00Z")
    b.record_fix_scan("duplicate", by="agent", duplicate_of=G1, evidence="same")
    seed(G3, state="published", cve_id="CVE-2026-41679")
    monkeypatch.setattr(adv_mod, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(adv_mod, "_synced_store_root", None)
    monkeypatch.setattr(adv_mod, "_store_pr_states", lambda: ({10: "merged"}, False))
    yield store
    adv_mod.STORE_ROOT = None
    adv_mod._synced_store_root = None
    advisory_data.set_store_root(None)


def test_list_rows_newest_first_with_verdict_fields(seeded):
    rows, loading = adv_mod.list_advisories()
    assert loading is False
    assert [r["ghsa_id"] for r in rows] == [G1, G2, G3]
    first = rows[0]
    assert first["verdict"] == "fixed" and first["fix_commit"] == "c647b8cc2ea6"
    assert first["links"][0]["state"] == "merged" and first["link_count"] == 1
    assert rows[1]["duplicate_of"] == G1
    assert "description" not in first


def test_query_filters_state_verdict_and_text(seeded):
    assert [r["ghsa_id"] for r in adv_mod.query_advisories(state="triage")["items"]] == [G1, G2]
    assert len(adv_mod.query_advisories(state=["all", "triage"])["items"]) == 3
    assert [r["ghsa_id"] for r in adv_mod.query_advisories(state=["triage", "closed"])["items"]] == [G1, G2]
    assert [r["ghsa_id"] for r in adv_mod.query_advisories(verdict="duplicate")["items"]] == [G2]
    assert [r["ghsa_id"] for r in adv_mod.query_advisories(verdict="none")["items"]] == [G3]
    assert [r["ghsa_id"] for r in adv_mod.query_advisories(q="bob")["items"]] == [G2]
    assert [r["ghsa_id"] for r in adv_mod.query_advisories(q="cve-2026")["items"]] == [G3]
    out = adv_mod.query_advisories(sort="severity", direction="desc", limit=1)
    assert out["total"] == 3 and out["items"][0]["ghsa_id"] == G1


def test_detail_carries_description_and_404s_on_unknown(seeded):
    d = adv_mod.get_advisory(G1)
    assert d is not None and d["description"] == "## Details\nbody"
    assert d["fix_scan"]["evidence"] == "gone"
    assert adv_mod.get_advisory("GHSA-2222-2222-2229") is None
    assert adv_mod.get_advisory("not-a-ghsa") is None


def test_sources_available_probes_advisories(monkeypatch):
    from prospector_app.backend import alerts as alerts_mod
    from prospector_app.backend import executor
    monkeypatch.setattr(alerts_mod, "_sources_cache", None)
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "tok")
    seen: list[str] = []

    def fake_read(path, token, params=None, *, source=""):
        seen.append(path)
        if path.endswith("/security-advisories"):
            raise RuntimeError("HTTP 403")
        return []

    monkeypatch.setattr(alerts_mod.alert_config, "gh_alert_read", fake_read)
    out = alerts_mod.sources_available()
    assert out["advisory"] is False and out["code-scanning"] is True
    assert any(p.endswith("/security-advisories") for p in seen)
    alerts_mod._sources_cache = None
