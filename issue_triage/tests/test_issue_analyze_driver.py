"""ANALYZE driver: pending selection + verdict application (the pure halves)."""
from issue_triage import issue_analyze_driver
from issue_triage import issue_freshness
from issue_triage import issue_store

META = {"title": "t", "body": "b", "state": "open", "updated_at": "T1"}


def test_apply_verdicts_routes_and_stamps(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(4, META)
    st.create_issue(5, META)
    issue_analyze_driver.apply_verdicts(st, [
        {"issue": 5, "disposition": "close-dup", "canonical": 4, "rationale": "dup of #4",
         "gist": "User can't do X because Y is broken."},
    ])
    iss = st.load_issue(5)
    assert iss.disposition == "close-dup"
    assert iss.canonical == 4
    assert iss.gist == "User can't do X because Y is broken."
    assert issue_freshness.is_current(iss, "analysis")


def test_apply_verdicts_without_gist(tmp_path):
    """A verdict that omits gist still applies; gist is None."""
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, META)
    issue_analyze_driver.apply_verdicts(st, [
        {"issue": 5, "disposition": "needs-human", "rationale": "judgement call"}])
    assert st.load_issue(5).gist is None


def test_apply_verdicts_rejects_bad_disposition(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, META)
    try:
        issue_analyze_driver.apply_verdicts(st, [{"issue": 5, "disposition": "merge"}])
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_pending_selects_only_stale_or_missing(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    fresh = st.create_issue(4, META)
    fresh.route_to("needs-human", "n")          # current analysis
    st.create_issue(5, META)                    # no analysis yet
    assert issue_analyze_driver.pending(st) == [5]


def test_pending_reincludes_after_issue_update(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    iss.route_to("needs-human", "n")
    assert issue_analyze_driver.pending(st) == []
    iss.set_meta({**META, "updated_at": "T2"})  # new activity -> analysis stale
    assert issue_analyze_driver.pending(st) == [5]


def test_bundle_includes_cluster_context(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(4, META)
    st.create_issue(5, META)
    cl = st.create_issue_cluster(1, "c")
    cl.set_members([4, 5])
    cl.set_pain(7.0)
    b = {e["number"]: e for e in issue_analyze_driver.bundle(st)}
    assert b[5]["cluster"]["id"] == 1
    assert sorted(b[5]["cluster"]["members"]) == [4, 5]
    assert b[5]["cluster"]["pain"] == 7.0


def test_bundle_carries_author_trust(tmp_path, monkeypatch):
    from pipeline import profile
    monkeypatch.setattr(profile, "active",
                        lambda: profile.RepoProfile(trusted_authors=("maint",)))
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(4, dict(META, author="maint"))
    st.create_issue(5, dict(META, author="rando"))
    b = {e["number"]: e for e in issue_analyze_driver.bundle(st)}
    assert b[4]["author"] == "maint" and b[4]["trusted_author"] is True
    assert b[5]["author"] == "rando" and b[5]["trusted_author"] is False


def test_bundle_annotates_candidate_pr_state(tmp_path):
    """Each candidate PR carries its current state, so link-pr can require an open
    one; a PR absent from the state map is "unknown", never open."""
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    iss.set_links([{"pr": 7, "how": "explicit", "title": "fix"},
                   {"pr": 8, "how": "fix-found", "title": "landed"},
                   {"pr": 9, "how": "subsystem", "title": "gone"}])
    b = issue_analyze_driver.bundle(st, pr_states={7: "open", 8: "merged"})
    assert [(c["pr"], c["state"]) for c in b[0]["candidate_prs"]] == [
        (7, "open"), (8, "merged"), (9, "unknown")]


def test_bundle_without_pr_states_marks_every_candidate_unknown(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    iss.set_links([{"pr": 7, "how": "explicit", "title": "fix"}])
    b = issue_analyze_driver.bundle(st)
    assert b[0]["candidate_prs"] == [
        {"pr": 7, "how": "explicit", "title": "fix", "state": "unknown"}]


def test_bundle_only_restricts_to_named_issues(tmp_path):
    """`only` bundles exactly the named issues (unknown numbers skipped), letting
    the headless path batch pending issues across several calls."""
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(4, META)
    st.create_issue(5, META)
    st.create_issue(6, META)
    b = issue_analyze_driver.bundle(st, only=[6, 4, 99])
    assert [e["number"] for e in b] == [6, 4]


def test_analyze_close_dup_supersedes_current_close_fixed(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    iss.record_fixed(42, rationale="already fixed by #42")
    assert issue_freshness.is_current(st.load_issue(5), "fix_scan")
    issue_analyze_driver.apply_verdicts(st, [
        {"issue": 5, "disposition": "close-dup", "canonical": 4, "rationale": "dup"}])
    assert st.load_issue(5).disposition == "close-dup"  # close-dup outranks close-fixed


def test_analyze_keep_open_does_not_supersede_current_close_fixed(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    iss.record_fixed(42, rationale="already fixed by #42")
    assert issue_freshness.is_current(st.load_issue(5), "fix_scan")
    issue_analyze_driver.apply_verdicts(st, [
        {"issue": 5, "disposition": "request-repro", "rationale": "need info", "asks": ["steps?"]}])
    assert st.load_issue(5).disposition == "close-fixed"  # keep-open can't downgrade a fixed finding


def test_analyze_reevaluates_stale_close_fixed(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    iss.record_fixed(42, rationale="fixed by #42")
    iss.set_meta({**META, "updated_at": "T2"})  # issue edited upstream -> fix_scan goes stale
    assert not issue_freshness.is_current(st.load_issue(5), "fix_scan")
    issue_analyze_driver.apply_verdicts(st, [
        {"issue": 5, "disposition": "close-dup", "canonical": 4, "rationale": "dup"}])
    assert st.load_issue(5).disposition == "close-dup"  # guard did NOT protect the stale finding


def test_prompt_and_criteria_are_shared_canon():
    """The headless batch prompt embeds the canonical criteria bullets, asks for
    both a gist and a rationale, and keeps the per-call bundle placeholder."""
    assert issue_analyze_driver.DISPOSITION_CRITERIA in issue_analyze_driver.ANALYZE_PROMPT
    assert "__BUNDLE_PATH__" in issue_analyze_driver.ANALYZE_PROMPT
    assert "__REPO__" not in issue_analyze_driver.ANALYZE_PROMPT
    assert '"gist"' in issue_analyze_driver.ANALYZE_PROMPT
    assert '"rationale"' in issue_analyze_driver.ANALYZE_PROMPT
    for disp in sorted(issue_analyze_driver.VALID):
        assert disp in issue_analyze_driver.DISPOSITION_CRITERIA


def test_analyze_needs_human_does_not_supersede_current_close_fixed(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    iss.record_fixed(42, rationale="already fixed by #42")
    assert issue_freshness.is_current(st.load_issue(5), "fix_scan")
    issue_analyze_driver.apply_verdicts(st, [
        {"issue": 5, "disposition": "needs-human", "rationale": "judgement call"}])
    assert st.load_issue(5).disposition == "close-fixed"  # needs-human can't downgrade a found fix
