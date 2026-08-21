"""Advisory: typed accessors, single-write mutations, freshness stamping."""
from alert_triage.advisory_model import Advisory
from alert_triage.advisory_store import AdvisoryStore, advisory_id
from alert_triage.alert_freshness import is_current


def _meta(ghsa: str = "GHSA-7f7c-55pc-67wg", **over) -> dict:
    meta = {
        "ghsa_id": ghsa, "state": "triage", "severity": "high",
        "summary": "Host header forwarding bypasses auth", "description": "long text",
        "cve_id": None, "cwe_ids": [], "reporter": "vikychoi", "author": "vikychoi",
        "created_at": "2026-08-20T08:09:42Z", "updated_at": "2026-08-20T08:09:42Z",
        "published_at": None, "closed_at": None,
        "html_url": f"https://github.com/o/r/security/advisories/{ghsa}",
        "vulnerable_range": None, "patched_versions": None,
    }
    meta.update(over)
    return meta


def _seed(store: AdvisoryStore, ghsa: str = "GHSA-7f7c-55pc-67wg", **over) -> Advisory:
    a = Advisory(store, {"id": advisory_id(ghsa)})
    a.apply_facts(_meta(ghsa, **over))
    return a


def test_apply_facts_persists_meta_and_links(tmp_path):
    store = AdvisoryStore(tmp_path)
    a = Advisory(store, {"id": advisory_id("GHSA-7f7c-55pc-67wg")})
    a.apply_facts(_meta(), links=[{"kind": "pr", "number": 3, "how": "text-ref",
                                   "state": "merged"}])
    back = store.load_advisory(a.id)
    assert back is not None
    assert back.summary == "Host header forwarding bypasses auth"
    assert back.reporter == "vikychoi"
    assert [c["number"] for c in back.candidates] == [3]
    assert is_current(back, "links")


def test_record_fix_scan_duplicate_and_fixed(tmp_path):
    store = AdvisoryStore(tmp_path)
    a = _seed(store)
    a.record_fix_scan("duplicate", by="deterministic",
                      duplicate_of="GHSA-2222-2222-2223", evidence="CVE follow-up")
    back = store.load_advisory(a.id)
    assert back is not None and back.verdict == "duplicate"
    assert back.duplicate_of == "GHSA-2222-2222-2223"
    assert back.fix_scan == {"verdict": "duplicate", "by": "deterministic",
                             "duplicate_of": "GHSA-2222-2222-2223",
                             "evidence": "CVE follow-up"}
    assert is_current(back, "fix_scan", max_age_days=7)
    b = _seed(store, "GHSA-2222-2222-2223")
    b.record_fix_scan("fixed", by="agent", fix_commit="c647b8cc2ea6",
                      evidence="removed", links=[{"kind": "pr", "number": 9,
                                                  "how": "agent"}])
    back = store.load_advisory(b.id)
    assert back is not None and back.fix_commit == "c647b8cc2ea6"
    assert [c["number"] for c in back.candidates] == [9]


def test_fix_scan_goes_stale_when_updated_at_moves(tmp_path):
    store = AdvisoryStore(tmp_path)
    a = _seed(store)
    a.record_fix_scan("not-fixed", by="agent")
    assert is_current(a, "fix_scan")
    a.apply_facts(_meta(updated_at="2026-08-21T00:00:00Z"))
    assert not is_current(a, "fix_scan")
