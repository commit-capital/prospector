"""CLUSTER driver: deterministic membership + pain into the store."""
from issue_triage import issue_cluster_driver
from issue_triage import issue_store

META = {"title": "Login crash", "body": "loginButton broken", "state": "open",
        "updated_at": "T1", "labels": ["bug"], "comments": 1, "reactions_total": 5,
        "thumbs_up": 5, "author": "a", "created_at": "2026-06-01T00:00:00Z"}


def test_cluster_driver_assigns_membership_and_pain(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    for n in (4, 5, 6):
        iss = st.create_issue(n, META)
        iss.set_summary("auth", ["loginButton"])  # shared rare identifier -> one cluster
    n_clusters = issue_cluster_driver.run(st)
    assert n_clusters >= 1
    clusters = st.all_issue_clusters()
    cl = next(c for c in clusters.values() if set(c.members) == {4, 5, 6})
    assert cl.pain is not None
    assert not cl.needs_review
    for n in cl.members:
        assert st.load_issue(n).cluster_id == cl.id


def test_distinct_reporters_outrank_same_author_refiles(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    issues = {}
    for n in (1, 2, 3, 4):
        issues[n] = st.create_issue(n, {**META, "author": "solo"})
    for i, n in enumerate((11, 12, 13, 14)):
        issues[n] = st.create_issue(n, {**META, "author": f"u{i}"})
    groups = [{"id": 1, "members": [1, 2, 3, 4], "needs_review": False},
              {"id": 2, "members": [11, 12, 13, 14], "needs_review": False}]
    pain = issue_cluster_driver.rank_pain(groups, issues)
    # equal engagement either way; four distinct reporters beat one author x4
    assert pain[2] > pain[1]


def test_oversized_chain_flagged_needs_review(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    # 13 issues chained by rare pairwise-shared identifiers -> one component > SIZE_FLAG(12)
    for i in range(1, 14):
        iss = st.create_issue(i, {**META, "title": f"bug {i}"})
        iss.set_summary("auth", [f"id{i}", f"id{i + 1}"])
    issue_cluster_driver.run(st)
    big = [c for c in st.all_issue_clusters().values() if len(c.members) > 12]
    assert big, "expected a chained cluster larger than SIZE_FLAG"
    assert all(c.needs_review for c in big)


def test_rerun_preserves_confirmed_curation(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    # two issues with NO shared identifier -> they would not deterministically cluster
    a = st.create_issue(4, {**META, "title": "alpha"})
    a.set_summary("auth", ["alphaId"])
    b = st.create_issue(5, {**META, "title": "beta"})
    b.set_summary("inbox", ["betaId"])
    # a human-confirmed cluster joining them
    cl = st.create_issue_cluster(1, "curated group")
    cl.set_members([4, 5])
    cl.record_curation({"confirmed": True, "canonical": 4})
    issue_cluster_driver.run(st)
    # the curated cluster survives with its membership + canonical intact
    kept = st.load_issue_cluster(1)
    assert kept is not None
    assert sorted(kept.members) == [4, 5]
    assert kept.canonical == 4
    assert st.load_issue(5).cluster_id == 1


def test_rerun_moves_backref_when_membership_changes(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    for n in (4, 5):
        iss = st.create_issue(n, META)
        iss.set_summary("auth", ["loginButton"])
    issue_cluster_driver.run(st)
    together = st.load_issue(5).cluster_id
    assert together == st.load_issue(4).cluster_id
    # issue 5 stops sharing the identifier -> it re-clusters alone
    st.edit_issue(5).set_summary("auth", ["logoutButton"])
    issue_cluster_driver.run(st)
    apart = st.load_issue(5).cluster_id
    assert apart != st.load_issue(4).cluster_id
    assert st.load_issue_cluster(apart).members == [5]
    assert 4 in st.load_issue_cluster(st.load_issue(4).cluster_id).members


def test_rerun_restamps_curated_pain(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    a = st.create_issue(4, {**META, "title": "alpha"})
    a.set_summary("auth", ["alphaId"])
    b = st.create_issue(5, {**META, "title": "beta"})
    b.set_summary("inbox", ["betaId"])
    cl = st.create_issue_cluster(1, "curated group")
    cl.set_members([4, 5])
    cl.record_curation({"confirmed": True, "canonical": 4})
    cl.set_pain(99.0)
    issue_cluster_driver.run(st)
    kept = st.load_issue_cluster(1)
    # curated membership survives, but pain is recomputed in the shared population
    # (the blend is in [0,1] x severity multiplier <= 2, so 99.0 cannot persist)
    assert kept.pain is not None and kept.pain <= 2.0


def test_rerun_rebuilds_cleanly(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    for n in (4, 5, 6):
        iss = st.create_issue(n, META)
        iss.set_summary("auth", ["loginButton"])
    issue_cluster_driver.run(st)
    first = len(st.all_issue_clusters())
    issue_cluster_driver.run(st)  # idempotent rebuild
    assert len(st.all_issue_clusters()) == first
    # backrefs still consistent after rebuild
    for n in (4, 5, 6):
        cid = st.load_issue(n).cluster_id
        assert cid is not None and n in st.load_issue_cluster(cid).members
