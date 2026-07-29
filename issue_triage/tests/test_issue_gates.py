"""issue_gates: close-as-dup policy + derived cluster state."""
from issue_triage import issue_gates
from issue_triage import issue_store

META = {"title": "t", "state": "open", "updated_at": "T1"}


def _confirmed_dup(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(4, META)
    iss = st.create_issue(5, META)
    cl = st.create_issue_cluster(1, "c")
    cl.set_members([4, 5])
    cl.record_curation({"confirmed": True, "canonical": 4})
    iss.route_to("close-dup", "dup of #4", canonical=4)
    return st, st.load_issue(5), st.load_issue_cluster(1)


def test_allowed_for_confirmed_dup(tmp_path):
    st, iss, cl = _confirmed_dup(tmp_path)
    ok, reason = issue_gates.close_dup_allowed(iss, cl)
    assert ok, reason


def test_blocked_when_needs_review(tmp_path):
    st, iss, cl = _confirmed_dup(tmp_path)
    cl.set_needs_review(True)
    ok, reason = issue_gates.close_dup_allowed(iss, st.load_issue_cluster(1))
    assert not ok and "needs-review" in reason


def test_blocked_when_not_close_dup(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    iss.route_to("needs-human", "unclear")
    ok, reason = issue_gates.close_dup_allowed(st.load_issue(5), None)
    assert not ok and "close-dup" in reason


def test_blocked_when_canonical_is_self(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    # bypass model (route_to forbids nothing here) — craft analysis pointing at self
    iss.route_to("close-dup", "self", canonical=5)
    ok, reason = issue_gates.close_dup_allowed(st.load_issue(5), None)
    assert not ok and "itself" in reason


def test_blocked_when_curation_unconfirmed(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(4, META)
    iss = st.create_issue(5, META)
    cl = st.create_issue_cluster(1, "c")
    cl.set_members([4, 5])
    iss.route_to("close-dup", "dup of #4", canonical=4)
    ok, reason = issue_gates.close_dup_allowed(st.load_issue(5), st.load_issue_cluster(1))
    assert not ok and "confirm" in reason.lower()


def test_blocked_when_issue_closed_in_store(tmp_path):
    st, iss, cl = _confirmed_dup(tmp_path)
    dup = st.edit_issue(5)
    dup.set_meta({**META, "state": "closed"})
    ok, reason = issue_gates.close_dup_allowed(st.load_issue(5), cl)
    assert not ok and "already closed" in reason


def test_eligibility_blocks_closed_canonical(tmp_path):
    st, iss, cl = _confirmed_dup(tmp_path)
    canon = st.edit_issue(4)
    canon.set_meta({**META, "state": "closed"})
    ok, reason = issue_gates.close_dup_eligibility(iss, cl, st.all_issues())
    assert not ok and "canonical" in reason.lower()


def test_eligibility_allows_canonical_closed_as_completed(tmp_path):
    """A fixed canonical remains a valid explanation for its duplicates."""
    st, iss, cl = _confirmed_dup(tmp_path)
    canon = st.edit_issue(4)
    canon.set_meta({**META, "state": "closed", "state_reason": "completed"})
    ok, reason = issue_gates.close_dup_eligibility(iss, cl, st.all_issues())
    assert ok, reason
    assert "fixed" in reason


def test_eligibility_passes_with_open_canonical(tmp_path):
    st, iss, cl = _confirmed_dup(tmp_path)
    ok, reason = issue_gates.close_dup_eligibility(iss, cl, st.all_issues())
    assert ok, reason


def test_eligibility_blocks_live_closed_dup(tmp_path):
    """#411: the store says open but the duplicate itself is closed upstream —
    the live check blocks it."""
    st, iss, cl = _confirmed_dup(tmp_path)
    ok, reason = issue_gates.close_dup_eligibility(
        iss, cl, st.all_issues(), live_state=lambda n: "closed" if n == 5 else "open")
    assert not ok and "#5" in reason and "already closed" in reason


def test_eligibility_blocks_live_closed_canonical(tmp_path):
    """#411: the store says open but the canonical is closed upstream — the live
    state wins over the store snapshot."""
    st, iss, cl = _confirmed_dup(tmp_path)
    ok, reason = issue_gates.close_dup_eligibility(
        iss, cl, st.all_issues(), live_state=lambda n: "open" if n == 5 else "closed")
    assert not ok and "canonical" in reason.lower()


def test_eligibility_allows_live_canonical_closed_as_completed(tmp_path):
    """Live state + reason override the stale open store snapshot."""
    st, iss, cl = _confirmed_dup(tmp_path)
    ok, reason = issue_gates.close_dup_eligibility(
        iss, cl, st.all_issues(),
        live_state=lambda n: "open" if n == 5 else "closed",
        live_state_reason=lambda n: "completed")
    assert ok, reason


def test_eligibility_blocks_live_canonical_closed_not_planned(tmp_path):
    st, iss, cl = _confirmed_dup(tmp_path)
    ok, reason = issue_gates.close_dup_eligibility(
        iss, cl, st.all_issues(),
        live_state=lambda n: "open" if n == 5 else "closed",
        live_state_reason=lambda n: "not_planned")
    assert not ok and "not_planned" in reason


def test_eligibility_fails_open_when_live_unreachable(tmp_path):
    """A live fetcher that returns None (GitHub unreachable) falls back to the
    store's state and does not block."""
    st, iss, cl = _confirmed_dup(tmp_path)
    ok, reason = issue_gates.close_dup_eligibility(iss, cl, st.all_issues(), live_state=lambda n: None)
    assert ok, reason


def test_eligibility_skips_live_fetch_when_statically_blocked(tmp_path):
    """The live fetcher is only consulted once the static checks pass."""
    st, iss, cl = _confirmed_dup(tmp_path)
    cl.set_needs_review(True)
    calls: list[int] = []

    def fetch(n: int) -> str | None:
        calls.append(n)
        return "open"

    ok, _ = issue_gates.close_dup_eligibility(iss, st.load_issue_cluster(1), st.all_issues(), live_state=fetch)
    assert not ok and calls == []


def test_cluster_state_dup_ready(tmp_path):
    st, iss, cl = _confirmed_dup(tmp_path)
    assert issue_gates.issue_cluster_state(cl, st.all_issues()) == "dup-ready"


def test_cluster_state_needs_curation_when_unconfirmed(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(4, META)
    st.create_issue(5, META)
    cl = st.create_issue_cluster(1, "c")
    cl.set_members([4, 5])
    assert issue_gates.issue_cluster_state(cl, st.all_issues()) == "needs-curation"


def test_disposition_outranks_matches_issue_precedence():
    assert issue_gates.disposition_outranks("close-dup", "close-fixed")       # close-dup wins
    assert not issue_gates.disposition_outranks("close-fixed", "close-dup")   # close-fixed loses to dup
    assert issue_gates.disposition_outranks("close-fixed", "needs-human")     # a found fix beats needs-human
    assert not issue_gates.disposition_outranks("needs-human", "close-fixed")  # needs-human loses to a found fix
    assert issue_gates.disposition_outranks("close-fixed", "request-repro")   # a close beats keep-open
    assert not issue_gates.disposition_outranks("request-repro", "close-fixed")  # keep-open loses to a close
    assert issue_gates.disposition_outranks("close-fixed", None)              # None is always replaced
