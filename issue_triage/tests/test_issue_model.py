"""Issue + IssueCluster: typed mutators stamp freshness and own the membership link."""
from issue_triage import issue_freshness
from issue_triage import issue_store

META = {"title": "Crash on save", "state": "open", "author": "alice",
        "updated_at": "T1", "labels": ["bug"], "comments": 2,
        "reactions_total": 3, "thumbs_up": 1, "url": "u"}


def test_route_to_stamps_against_updated_at(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    iss.route_to("close-dup", "dup of #4", canonical=4)
    reloaded = st.load_issue(5)
    assert reloaded.disposition == "close-dup"
    assert reloaded.canonical == 4
    assert reloaded.rationale == "dup of #4"
    assert issue_freshness.is_current(reloaded, "analysis")


def test_analysis_goes_stale_when_updated_at_moves(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    iss.route_to("needs-human", "unclear")
    iss.set_meta({**META, "updated_at": "T2"})  # new activity on the issue
    assert not issue_freshness.is_current(st.load_issue(5), "analysis")


def test_set_summary_and_repro(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    iss.set_summary("auth", ["loginButton"])
    iss.set_repro({"grade": "B", "score": 3})
    r = st.load_issue(5)
    assert r.subsystem == "auth"
    assert r.identifiers == ["loginButton"]
    assert r.repro_grade == "B"
    assert issue_freshness.is_current(r, "summary")
    assert issue_freshness.is_current(r, "repro")


def test_cluster_membership_two_way_link(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, META)
    st.create_issue(6, {**META, "title": "dupe"})
    cl = st.create_issue_cluster(1, "Crash cluster")
    cl.set_members([5, 6])
    assert st.load_issue(5).cluster_id == 1
    assert st.load_issue(6).cluster_id == 1
    assert sorted(st.load_issue_cluster(1).members) == [5, 6]


def test_remove_member_clears_backref(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, META)
    cl = st.create_issue_cluster(1, "Crash cluster")
    cl.set_members([5])
    cl.set_members([])  # remove 5
    assert st.load_issue(5).cluster_id is None
    assert st.load_issue_cluster(1).members == []


def test_record_curation_sets_confirmed_and_canonical(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    cl = st.create_issue_cluster(1, "Crash cluster")
    cl.record_curation({"confirmed": True, "canonical": 5})
    reloaded = st.load_issue_cluster(1)
    assert reloaded.curation == {"confirmed": True, "canonical": 5}
    assert reloaded.canonical == 5


def test_record_fixed_sets_disposition_link_and_scan(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, {"title": "t", "state": "open", "updated_at": "T1"})
    iss.record_fixed(42, rationale="diff at src/x.ts adds the null guard the issue asks for",
                     gist="Crash when X is null.", upstream_date="2026-05-01", title="fix null")
    got = st.load_issue(5)
    assert got.disposition == "close-fixed"
    assert got.fixed_by == 42
    assert got.fix_scan == {"status": "fixed", "fixed_by": 42, "upstream_date": "2026-05-01",
                            "gist": "Crash when X is null.",
                            "rationale": "diff at src/x.ts adds the null guard the issue asks for"}
    assert {"pr": 42, "how": "fix-found", "title": "fix null"} in got.candidate_prs
    assert issue_freshness.is_current(got, "fix_scan")


def test_record_fixed_link_is_idempotent(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, {"title": "t", "state": "open", "updated_at": "T1"})
    iss.record_fixed(42, rationale="r")
    iss.record_fixed(42, rationale="r")
    fix_links = [c for c in st.load_issue(5).candidate_prs if c.get("how") == "fix-found"]
    assert len(fix_links) == 1


def test_record_fixed_without_disposition_keeps_analysis(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, {"title": "t", "state": "open", "updated_at": "T1"})
    iss.route_to("close-dup", "dup", canonical=4)
    iss.record_fixed(42, rationale="also fixed by #42", set_disposition=False)
    got = st.load_issue(5)
    assert got.disposition == "close-dup"          # higher-precedence disposition preserved
    assert got.canonical == 4                       # and its canonical
    assert got.fix_scan["status"] == "fixed"        # evidence still recorded
    assert any(c.get("how") == "fix-found" and c["pr"] == 42 for c in got.candidate_prs)


def test_record_fix_scan_touches_only_fix_scan(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, {"title": "t", "state": "open", "updated_at": "T1"})
    iss.route_to("needs-human", "prior")
    iss.record_fix_scan("likely-fixed", gist="Looks fixed on main.", rationale="no PR to cite")
    got = st.load_issue(5)
    assert got.disposition == "needs-human"  # unchanged
    assert got.fix_scan == {"status": "likely-fixed", "gist": "Looks fixed on main.",
                            "rationale": "no PR to cite"}
