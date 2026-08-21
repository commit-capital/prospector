"""advisory_ingest: payload normalization, upsert-on-change, links for open
states only."""
from alert_triage import advisory_ingest
from alert_triage.advisory_store import AdvisoryStore, advisory_id

RAW = {
    "ghsa_id": "GHSA-7f7c-55pc-67wg", "state": "triage", "severity": None,
    "summary": "Attacker-controlled Host forwarding", "description": "## Report\nlong",
    "cve_id": None, "cwe_ids": ["CWE-346"], "author": {"login": "vikychoi"},
    "credits": [{"login": "vikychoi", "type": "reporter"}],
    "collaborating_users": [{"login": "vikychoi"}],
    "created_at": "2026-08-20T08:09:42Z", "updated_at": "2026-08-20T08:09:42Z",
    "published_at": None, "closed_at": None,
    "html_url": "https://github.com/o/r/security/advisories/GHSA-7f7c-55pc-67wg",
    "vulnerabilities": [{"package": {"name": "o/r", "ecosystem": ""},
                         "vulnerable_version_range": "2026.817.0",
                         "patched_versions": ""}],
}

PRS = [{"number": 10, "title": "Fix GHSA-7f7c-55pc-67wg host forwarding", "body": "",
        "state": "merged", "head_sha": "aaa"}]


def test_normalize_maps_fields_and_unknown_severity():
    meta = advisory_ingest.normalize(RAW)
    assert meta["ghsa_id"] == "GHSA-7f7c-55pc-67wg"
    assert meta["state"] == "triage" and meta["severity"] == "unknown"
    assert meta["reporter"] == "vikychoi" and meta["author"] == "vikychoi"
    assert meta["vulnerable_range"] == "2026.817.0" and meta["patched_versions"] is None
    assert meta["cwe_ids"] == ["CWE-346"]
    assert meta["description"] == "## Report\nlong"


def test_normalize_reporter_falls_back_to_collaborator_then_author():
    raw = {**RAW, "credits": [], "collaborating_users": [{"login": "bennati"}]}
    assert advisory_ingest.normalize(raw)["reporter"] == "bennati"
    raw = {**RAW, "credits": [], "collaborating_users": []}
    assert advisory_ingest.normalize(raw)["reporter"] == "vikychoi"


def test_new_advisory_lands_with_meta_and_text_ref_links(tmp_path):
    store = AdvisoryStore(tmp_path)
    metas = [advisory_ingest.normalize(RAW)]
    assert advisory_ingest.ingest_records(store, metas, PRS, {}) == 1
    a = store.load_advisory(advisory_id("GHSA-7f7c-55pc-67wg"))
    assert a is not None and a.state == "triage"
    assert [(c["number"], c["how"]) for c in a.candidates] == [(10, "text-ref")]


def test_unchanged_reingest_writes_nothing(tmp_path):
    store = AdvisoryStore(tmp_path)
    metas = [advisory_ingest.normalize(RAW)]
    advisory_ingest.ingest_records(store, metas, PRS, {})
    assert advisory_ingest.ingest_records(store, metas, PRS, {}) == 0


def test_closed_advisory_keeps_prior_links_and_fix_scan(tmp_path):
    store = AdvisoryStore(tmp_path)
    advisory_ingest.ingest_records(store, [advisory_ingest.normalize(RAW)], PRS, {})
    a = store.edit_advisory(advisory_id("GHSA-7f7c-55pc-67wg"))
    a.record_fix_scan("not-fixed", by="agent")
    closed = advisory_ingest.normalize({**RAW, "state": "closed",
                                        "updated_at": "2026-08-21T00:00:00Z"})
    assert advisory_ingest.ingest_records(store, [closed], [], {}) == 1
    back = store.load_advisory(a.id)
    assert back is not None and back.state == "closed"
    assert [c["number"] for c in back.candidates] == [10]
    assert back.verdict == "not-fixed"
