"""already_fixed(): tier-1 issues (close-fixed, current fix_scan, live-merged
fixer) surface pain-sorted with a prefilled comment; tier-2 (likely-fixed) list
surfaces separately; not-fixed issues appear in neither."""
from prospector_app.backend import issues
from issue_triage.issue_store import IssueStore


def _store(tmp_path):
    st = IssueStore(tmp_path)
    a = st.create_issue(5, {"title": "crash on null", "state": "open", "updated_at": "T1"})
    a.record_fixed(42, rationale="#42 adds the guard", gist="Crash on null.")
    b = st.create_issue(6, {"title": "maybe fixed", "state": "open", "updated_at": "T1"})
    b.record_fix_scan("likely-fixed", gist="Looks fixed.", rationale="no PR")
    c = st.create_issue(7, {"title": "still broken", "state": "open", "updated_at": "T1"})
    c.record_fix_scan("not-fixed")
    return st


def test_already_fixed_partitions_tiers(tmp_path, monkeypatch):
    st = _store(tmp_path)
    monkeypatch.setattr(issues, "_sync_store_root", lambda: None)
    monkeypatch.setattr(issues.issue_data, "full_issues", lambda: st.all_issues())
    monkeypatch.setattr(issues.issue_data, "clusters", lambda: st.all_issue_clusters())
    monkeypatch.setattr(issues, "_live_pr_states", lambda ns: {42: "merged"})
    out = issues.already_fixed()
    assert [g["number"] for g in out["fixed"]] == [5]
    assert out["fixed"][0]["fixed_by"] == 42
    assert out["fixed"][0]["comment"]  # prefilled fixed_issue_comment
    assert [g["number"] for g in out["likely_fixed"]] == [6]


def test_already_fixed_excludes_unmerged_fixer(tmp_path, monkeypatch):
    """A close-fixed issue whose fixer isn't live-merged is excluded from both lanes."""
    st = IssueStore(tmp_path)
    a = st.create_issue(5, {"title": "crash", "state": "open", "updated_at": "T1"})
    a.record_fixed(42, rationale="#42 adds the guard", gist="Crash.")
    b = st.create_issue(8, {"title": "not really", "state": "open", "updated_at": "T1"})
    b.record_fixed(50, rationale="#50", gist="Maybe.")
    monkeypatch.setattr(issues, "_sync_store_root", lambda: None)
    monkeypatch.setattr(issues.issue_data, "full_issues", lambda: st.all_issues())
    monkeypatch.setattr(issues.issue_data, "clusters", lambda: st.all_issue_clusters())
    monkeypatch.setattr(issues, "_live_pr_states", lambda ns: {42: "merged", 50: "closed"})
    out = issues.already_fixed()
    assert [g["number"] for g in out["fixed"]] == [5]  # 8 excluded: fixer #50 not merged
    assert all(g["number"] != 8 for g in out["likely_fixed"])


def test_already_fixed_drops_stale_likely_fixed(tmp_path, monkeypatch):
    """A likely-fixed verdict that went stale (issue edited upstream) drops out of
    the review lane, matching the fixed lane's freshness gate."""
    st = IssueStore(tmp_path)
    b = st.create_issue(6, {"title": "maybe fixed", "state": "open", "updated_at": "T1"})
    b.record_fix_scan("likely-fixed", gist="Looks fixed.", rationale="no PR")
    b.set_meta({"title": "maybe fixed", "state": "open", "updated_at": "T2"})  # -> fix_scan stale
    monkeypatch.setattr(issues, "_sync_store_root", lambda: None)
    monkeypatch.setattr(issues.issue_data, "full_issues", lambda: st.all_issues())
    monkeypatch.setattr(issues.issue_data, "clusters", lambda: st.all_issue_clusters())
    monkeypatch.setattr(issues, "_live_pr_states", lambda ns: {})
    out = issues.already_fixed()
    assert out["likely_fixed"] == []
