"""advisory_find_fixed: candidate ordering/freshness, the tier-0 duplicate
rule, bundle + roster shape, and verdict application."""
import pytest

from alert_triage import advisory_find_fixed as ff
from alert_triage.advisory_model import Advisory
from alert_triage.advisory_store import AdvisoryStore, advisory_id


def _meta(ghsa: str, **over) -> dict:
    meta = {
        "ghsa_id": ghsa, "state": "triage", "severity": "medium",
        "summary": f"report {ghsa}", "description": "text", "cve_id": None,
        "cwe_ids": [], "reporter": "r", "author": "r",
        "created_at": "2026-08-10T00:00:00Z", "updated_at": "2026-08-10T00:00:00Z",
        "published_at": None, "closed_at": None,
        "html_url": f"https://github.com/o/r/security/advisories/{ghsa}",
        "vulnerable_range": None, "patched_versions": None,
    }
    meta.update(over)
    return meta


def _seed(store: AdvisoryStore, ghsa: str, **over) -> Advisory:
    a = Advisory(store, {"id": advisory_id(ghsa)})
    a.apply_facts(_meta(ghsa, **over))
    return a


G1, G2, G3, G4, G5 = ("GHSA-2222-2222-2223", "GHSA-2222-2222-2224",
                      "GHSA-2222-2222-2225", "GHSA-2222-2222-2226",
                      "GHSA-2222-2222-2227")


def test_candidates_open_unscanned_severity_then_newest(tmp_path):
    store = AdvisoryStore(tmp_path)
    _seed(store, G1, severity="low")
    _seed(store, G2, severity="critical", created_at="2026-08-01T00:00:00Z")
    _seed(store, G3, severity="critical", created_at="2026-08-09T00:00:00Z")
    _seed(store, G4, severity="critical", state="published")
    scanned = _seed(store, G5, severity="high")
    scanned.record_fix_scan("not-fixed", by="agent")
    assert ff.candidates(store) == [advisory_id(G3), advisory_id(G2), advisory_id(G1)]


def test_tier0_cve_follow_up_is_a_duplicate(tmp_path):
    store = AdvisoryStore(tmp_path)
    _seed(store, G1, summary=f"CVE ID follow-up for existing {G2} (not a new disclosure)")
    _seed(store, G2, state="published")
    _seed(store, G3, summary="SSRF in skill import")
    out = ff.deterministic_duplicates(store)
    assert out == [{"id": advisory_id(G1), "verdict": "duplicate", "by": "deterministic",
                    "duplicate_of": G2,
                    "evidence": f"The summary names itself a CVE-id follow-up for {G2}."}]


def test_bundle_and_roster(tmp_path):
    store = AdvisoryStore(tmp_path)
    a = _seed(store, G1, severity="high", summary="SSRF")
    a.apply_facts(_meta(G1, severity="high", summary="SSRF"),
                  links=[{"kind": "pr", "number": 4, "how": "text-ref", "state": "open"}])
    _seed(store, G2, state="closed")
    entries = ff.bundle(store, only=[advisory_id(G1)])
    assert [e["ghsa_id"] for e in entries] == [G1]
    assert entries[0]["candidates"][0]["number"] == 4 and entries[0]["description"] == "text"
    roster = ff.roster(store)
    assert roster == [{"ghsa_id": G1, "state": "triage", "summary": "SSRF"},
                      {"ghsa_id": G2, "state": "closed", "summary": f"report {G2}"}]


def test_apply_verdicts_validates_and_records(tmp_path):
    store = AdvisoryStore(tmp_path)
    _seed(store, G1)
    _seed(store, G2)
    n = ff.apply_verdicts(store, [
        {"id": advisory_id(G1), "verdict": "fixed", "fix_commit": "c647b8cc2ea6",
         "evidence": "removed", "links": [{"kind": "pr", "number": 9, "how": "agent"}]},
        {"id": advisory_id(G2), "verdict": "duplicate", "duplicate_of": G1,
         "evidence": "same root cause"},
    ])
    assert n == 2
    a = store.load_advisory(advisory_id(G1))
    assert a is not None and a.fix_commit == "c647b8cc2ea6" and a.candidates[0]["number"] == 9
    b = store.load_advisory(advisory_id(G2))
    assert b is not None and b.duplicate_of == G1 and (b.fix_scan or {})["by"] == "agent"
    with pytest.raises(ValueError):
        ff.apply_verdicts(store, [{"id": advisory_id(G1), "verdict": "maybe"}])


def test_filter_batch_verdicts_drops_foreign_and_unknown():
    entries = [{"id": 1}, {"id": 2}]
    raw = [{"id": 1, "verdict": "not-fixed"}, {"id": 3, "verdict": "fixed"},
           {"id": 2, "verdict": "resolved"}, {"verdict": "fixed"}]
    assert ff.filter_batch_verdicts(entries, raw) == [{"id": 1, "verdict": "not-fixed"}]
