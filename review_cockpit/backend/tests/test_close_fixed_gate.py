"""close_fixed_gate accepts a detector-discovered (fix-found) fixer, not only an
explicit Fixes/Closes reference."""
from review_cockpit.backend import issues


def test_fix_found_candidate_is_accepted(tmp_path, monkeypatch):
    from issue_triage.issue_store import IssueStore
    st = IssueStore(tmp_path)
    iss = st.create_issue(5, {"title": "t", "state": "open", "updated_at": "T1"})
    iss.record_fixed(42, rationale="semantic match", title="fix")
    monkeypatch.setattr(issues, "_store", lambda: st)
    monkeypatch.setattr(issues, "_live_pr_states", lambda ns: {42: "merged"})
    monkeypatch.setattr(issues, "_live_state", lambda n: "open")
    ok, reason = issues.close_fixed_gate(5, 42)
    assert ok, reason


def test_unrelated_pr_still_rejected(tmp_path, monkeypatch):
    from issue_triage.issue_store import IssueStore
    st = IssueStore(tmp_path)
    st.create_issue(5, {"title": "t", "state": "open", "updated_at": "T1"})
    monkeypatch.setattr(issues, "_store", lambda: st)
    ok, reason = issues.close_fixed_gate(5, 999)
    assert not ok
    assert "is not a fix candidate" in reason


def test_fix_found_but_pr_not_merged_is_rejected(tmp_path, monkeypatch):
    """A recorded fix-found link does not authorize a close on its own — the gate
    still live-reverifies the PR is merged."""
    from issue_triage.issue_store import IssueStore
    st = IssueStore(tmp_path)
    iss = st.create_issue(5, {"title": "t", "state": "open", "updated_at": "T1"})
    iss.record_fixed(42, rationale="semantic match", title="fix")
    monkeypatch.setattr(issues, "_store", lambda: st)
    monkeypatch.setattr(issues, "_live_pr_states", lambda ns: {42: "open"})  # not merged
    monkeypatch.setattr(issues, "_live_state", lambda n: "open")
    ok, reason = issues.close_fixed_gate(5, 42)
    assert not ok
    assert "not merged" in reason
