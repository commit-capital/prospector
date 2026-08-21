"""AdvisoryStore: GHSA ⇄ integer key, validation, roundtrip, watermarks."""
import pytest

from alert_triage.advisory_store import (
    AdvisoryStore, GHSA_ALPHABET, advisory_id, ghsa_of, validate_advisory)
from pipeline import schema
from pipeline.storekit import ValidationError


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



def _meta(ghsa: str = "GHSA-7f7c-55pc-67wg", **over) -> dict:
    meta = {
        "ghsa_id": ghsa, "state": "triage", "severity": "high",
        "summary": "Host header forwarding bypasses auth", "description": "long text",
        "cve_id": None, "cwe_ids": ["CWE-346"], "reporter": "vikychoi",
        "author": "vikychoi", "created_at": "2026-08-20T08:09:42Z",
        "updated_at": "2026-08-20T08:09:42Z", "published_at": None, "closed_at": None,
        "html_url": f"https://github.com/o/r/security/advisories/{ghsa}",
        "vulnerable_range": "2026.817.0", "patched_versions": None,
    }
    meta.update(over)
    return meta


def _rec(ghsa: str = "GHSA-7f7c-55pc-67wg", **meta_over) -> dict:
    return {"id": advisory_id(ghsa), "meta": _meta(ghsa, **meta_over)}


def test_alphabet_is_githubs_21_symbols():
    assert GHSA_ALPHABET == "23456789cfghjkmpqrvwx"
    assert len(set(GHSA_ALPHABET)) == 21


def test_advisory_id_round_trips_and_orders():
    for ghsa in ("GHSA-2222-2222-2222", "GHSA-7f7c-55pc-67wg", "GHSA-xxxx-xxxx-xxxx"):
        assert ghsa_of(advisory_id(ghsa)) == ghsa
    assert advisory_id("GHSA-2222-2222-2222") == 0
    assert advisory_id("GHSA-2222-2222-2223") == 1
    assert advisory_id("GHSA-xxxx-xxxx-xxxx") == 21 ** 12 - 1
    assert advisory_id("GHSA-xxxx-xxxx-xxxx") < 2 ** 63


@pytest.mark.parametrize("bad", ["GHSA-7f7c-55pc-67w", "GHSA-7f7c-55pc-67w1",
                                 "ghsa-7f7c-55pc-67wg", "CVE-2026-41679", ""])
def test_advisory_id_rejects_malformed(bad):
    with pytest.raises(ValueError):
        advisory_id(bad)


def test_validate_accepts_minimal_record():
    validate_advisory(_rec())


def test_validate_rejects_bad_state_severity_and_id_mismatch():
    for field, value in (("state", "open"), ("severity", "warning")):
        rec = _rec(**{field: value})
        with pytest.raises(ValidationError, match=field):
            validate_advisory(rec)
    rec = _rec()
    rec["id"] = advisory_id("GHSA-2222-2222-2223")
    with pytest.raises(ValidationError, match="id"):
        validate_advisory(rec)


def test_validate_fix_scan_shape_rules():
    dup_without_target = _rec()
    dup_without_target["fix_scan"] = {"verdict": "duplicate", "by": "agent"}
    with pytest.raises(ValidationError, match="duplicate_of"):
        validate_advisory(dup_without_target)
    fixed_without_commit = _rec()
    fixed_without_commit["fix_scan"] = {"verdict": "fixed", "by": "agent"}
    with pytest.raises(ValidationError, match="fix_commit"):
        validate_advisory(fixed_without_commit)
    stray_target = _rec()
    stray_target["fix_scan"] = {"verdict": "not-fixed", "by": "agent",
                                "duplicate_of": "GHSA-2222-2222-2223"}
    with pytest.raises(ValidationError, match="duplicate_of"):
        validate_advisory(stray_target)
    bad_by = _rec()
    bad_by["fix_scan"] = {"verdict": "not-fixed", "by": "human"}
    with pytest.raises(ValidationError, match="by"):
        validate_advisory(bad_by)
    ok = _rec()
    ok["fix_scan"] = {"verdict": "fixed", "by": "agent", "fix_commit": "c647b8cc2ea6",
                      "evidence": "commit removes the forwarding"}
    validate_advisory(ok)


def test_store_roundtrip_and_watermark(tmp_path):
    store = AdvisoryStore(tmp_path)
    store.save_advisory(_rec())
    a = store.load_advisory(advisory_id("GHSA-7f7c-55pc-67wg"))
    assert a is not None and a.ghsa_id == "GHSA-7f7c-55pc-67wg" and a.state == "triage"
    delta, hi = store.advisories_since(None)
    assert set(delta) == {advisory_id("GHSA-7f7c-55pc-67wg")} and hi
    again, _ = store.advisories_since(hi)
    assert again == {}
    assert store.load_advisory(advisory_id("GHSA-2222-2222-2223")) is None
