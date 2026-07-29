"""issue_views: generated ISSUE-STATUS.md / SUMMARY.md from the store."""
from issue_triage import issue_store
from issue_triage import issue_views


def test_status_md_lists_top_pain_clusters(tmp_path, monkeypatch):
    monkeypatch.setattr(issue_views, "DISPLAY_NAME", "Example Project")
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, {"title": "Crash", "state": "open", "updated_at": "T"})
    cl = st.create_issue_cluster(1, "Crash cluster")
    cl.set_members([5])
    cl.set_pain(42.0)
    md = issue_views.status_md(st)
    assert md.startswith("# Example Project Issue Triage — Status\n")
    assert "Crash cluster" in md
    assert "42" in md
    assert "Pain leaderboard" in md


def test_summary_md_marks_confirmed(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(4, {"title": "canon", "state": "open", "updated_at": "T"})
    st.create_issue(5, {"title": "dup", "state": "open", "updated_at": "T"})
    cl = st.create_issue_cluster(1, "Boot crash")
    cl.set_members([4, 5])
    cl.set_pain(3.0)
    cl.record_curation({"confirmed": True, "canonical": 4})
    md = issue_views.summary_md(st)
    assert "Boot crash" in md
    assert "#4" in md
    assert "confirmed" in md
