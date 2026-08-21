"""AdvisoryStore: GHSA ⇄ integer key, validation, roundtrip, watermarks."""
from pipeline import schema


def test_advisories_table_is_declared_with_bigint_key():
    t = schema.advisories
    assert t.c.id.primary_key
    assert "BigInteger" in type(t.c.id.type).__name__ or "BIGINT" in str(t.c.id.type).upper()
    assert {c.name for c in t.columns} == {
        "id", "data", "ghsa_id", "state", "severity", "updated_at", "saved_at"}


def test_mirror_advisory_projects_hot_fields():
    rec = {"id": 7, "meta": {"ghsa_id": "GHSA-2222-2222-2229", "state": "triage",
                             "severity": "high", "updated_at": "2026-08-20T00:00:00Z"}}
    assert schema.mirror_advisory(rec) == {
        "id": 7, "ghsa_id": "GHSA-2222-2222-2229", "state": "triage",
        "severity": "high", "updated_at": "2026-08-20T00:00:00Z"}
