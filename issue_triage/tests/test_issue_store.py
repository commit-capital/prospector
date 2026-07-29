"""IssueStore contract: the ONLY accessor for issue_triage/store. Everything
validates on write; reads never need defensive parsing."""
import pytest

from issue_triage import issue_store

GOOD_META = {"title": "Bug", "state": "open", "updated_at": "2026-06-23T00:00:00Z"}


def test_create_and_load_issue(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, GOOD_META)
    assert iss.number == 5
    assert st.load_issue(5).title == "Bug"


def test_load_missing_is_none(tmp_path):
    assert issue_store.IssueStore(tmp_path).load_issue(404) is None


def test_unknown_section_preserved(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.save_issue({"issue": 1, "meta": dict(GOOD_META), "novel_section": {"x": 1}})
    iss = st.load_issue(1)
    iss.raw["meta"]["title"] = "edited"
    st.save_issue(iss.raw)
    assert st.load_issue(1).raw["novel_section"] == {"x": 1}


def test_bad_state_rejected(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    with pytest.raises(issue_store.ValidationError):
        st.save_issue({"issue": 1, "meta": {**GOOD_META, "state": "weird"}})


def test_missing_updated_at_rejected(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    with pytest.raises(issue_store.ValidationError):
        st.save_issue({"issue": 1, "meta": {"title": "t", "state": "open"}})


def test_close_dup_requires_canonical(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    with pytest.raises(issue_store.ValidationError):
        st.save_issue({"issue": 1, "meta": GOOD_META,
                       "analysis": {"disposition": "close-dup"}})


def test_bad_disposition_rejected(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    with pytest.raises(issue_store.ValidationError):
        st.save_issue({"issue": 1, "meta": GOOD_META,
                       "analysis": {"disposition": "merge"}})


def test_bad_repro_grade_rejected(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    with pytest.raises(issue_store.ValidationError):
        st.save_issue({"issue": 1, "meta": GOOD_META, "repro": {"grade": "Z"}})


def test_cluster_roundtrip(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    cl = st.create_issue_cluster(3, "Login broken")
    assert cl.id == 3
    assert st.load_issue_cluster(3).title == "Login broken"


def test_cluster_requires_members_list(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    with pytest.raises(issue_store.ValidationError):
        st.save_issue_cluster({"id": 1, "title": "x", "members": "no"})


def test_delete_issue_clusters_bulk(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    for cid in (1, 2, 3):
        st.create_issue_cluster(cid, f"cluster {cid}")
    st.delete_issue_clusters([1, 3, 404])  # missing ids are ignored
    assert sorted(st.all_issue_clusters()) == [2]
    st.delete_issue_clusters([])  # no-op
    assert sorted(st.all_issue_clusters()) == [2]


def test_save_issue_clusters_many_validates(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    good = {"id": 1, "title": "a", "members": [4], "canonical": None}
    with pytest.raises(issue_store.ValidationError):
        st.save_issue_clusters_many([good, {"id": 2, "title": "", "members": []}])
    st.save_issue_clusters_many([good])
    assert st.load_issue_cluster(1).members == [4]


def test_close_fixed_requires_fixed_by(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, {"title": "t", "state": "open", "updated_at": "T1"})
    iss = st.edit_issue(5)
    # close-fixed with a fixer validates
    iss.rec["analysis"] = {"disposition": "close-fixed", "rationale": "r", "fixed_by": 42}
    st.save_issue(iss.rec)  # no raise
    # close-fixed without a fixer is rejected
    iss.rec["analysis"] = {"disposition": "close-fixed", "rationale": "r"}
    with pytest.raises(issue_store.ValidationError):
        st.save_issue(iss.rec)


def test_fix_scan_status_validated(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, {"title": "t", "state": "open", "updated_at": "T1"})
    iss = st.edit_issue(5)
    iss.rec["fix_scan"] = {"status": "likely-fixed"}
    st.save_issue(iss.rec)  # no raise
    iss.rec["fix_scan"] = {"status": "bogus"}
    with pytest.raises(issue_store.ValidationError):
        st.save_issue(iss.rec)


def test_runs_ledger_appends(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.append_run({"phase": "ingest", "issues": 3})
    st.append_run({"phase": "cluster", "clusters": 1})
    assert [r.phase for r in st.runs()] == ["ingest", "cluster"]


def test_stale_schema_refuses_issue_writes(tmp_path):
    from pipeline import schema, storekit
    st = issue_store.IssueStore(tmp_path)
    st.save_issue({"issue": 1, "meta": dict(GOOD_META)})
    storekit._stamp_schema_version(st.engine, schema.STORE_SCHEMA_VERSION + 1)
    storekit.refresh_schema_guard(st.engine)
    with pytest.raises(storekit.StaleSchemaError):
        st.save_issue({"issue": 2, "meta": dict(GOOD_META)})
    assert st.load_issue(1) is not None
