"""Find-fixed driver: candidate selection (pain order), bundle, verdict application."""
from issue_triage import issue_fixed_driver, issue_store

META = {"title": "t", "body": "b", "state": "open", "updated_at": "T1"}


def test_apply_fixed_verdict_marks_close_fixed(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, META)
    n = issue_fixed_driver.apply_verdicts(st, [
        {"issue": 5, "status": "fixed", "fixed_by": 42, "upstream_date": "2026-05-01",
         "gist": "Crash on null.", "rationale": "#42 adds the guard", "fixed_title": "guard"}])
    assert n == 1
    iss = st.load_issue(5)
    assert iss.disposition == "close-fixed"
    assert iss.fixed_by == 42
    assert any(c.get("how") == "fix-found" and c["pr"] == 42 for c in iss.candidate_prs)


def test_apply_fixed_does_not_override_close_dup(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    iss.route_to("close-dup", "dup of #4", canonical=4)
    issue_fixed_driver.apply_verdicts(st, [
        {"issue": 5, "status": "fixed", "fixed_by": 42, "rationale": "#42 fixes it"}])
    got = st.load_issue(5)
    assert got.disposition == "close-dup"        # close-dup outranks close-fixed
    assert got.fix_scan["status"] == "fixed"     # evidence still recorded


def test_apply_fixed_sets_close_fixed_when_unranked_or_lower(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, META)                      # no disposition
    b = st.create_issue(6, META)
    b.route_to("request-repro", "need info")     # keep-open, lower than a close
    issue_fixed_driver.apply_verdicts(st, [
        {"issue": 5, "status": "fixed", "fixed_by": 42, "rationale": "r"},
        {"issue": 6, "status": "fixed", "fixed_by": 43, "rationale": "r"}])
    assert st.load_issue(5).disposition == "close-fixed"
    assert st.load_issue(6).disposition == "close-fixed"  # close-fixed beats request-repro


def test_apply_fixed_without_fixed_by_raises(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, META)
    try:
        issue_fixed_driver.apply_verdicts(st, [{"issue": 5, "status": "fixed", "rationale": "r"}])
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_apply_likely_and_notfixed_touch_only_scan(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, META)
    st.create_issue(6, META)
    issue_fixed_driver.apply_verdicts(st, [
        {"issue": 5, "status": "likely-fixed", "rationale": "no PR to cite"},
        {"issue": 6, "status": "not-fixed", "rationale": "still broken on main"}])
    assert st.load_issue(5).disposition is None
    assert st.load_issue(5).fix_scan["status"] == "likely-fixed"
    assert st.load_issue(6).fix_scan["status"] == "not-fixed"


def test_apply_rejects_bad_status(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, META)
    try:
        issue_fixed_driver.apply_verdicts(st, [{"issue": 5, "status": "merge"}])
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_deterministic_fixed_flags_merged_explicit_fixers(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    a = st.create_issue(5, META)
    a.set_links([{"pr": 42, "how": "explicit", "title": "fix"}])
    b = st.create_issue(6, META)
    b.set_links([{"pr": 43, "how": "explicit", "title": "wip"}])   # 43 is open, not merged
    c = st.create_issue(7, META)
    c.set_links([{"pr": 44, "how": "subsystem", "title": "same area"}])  # tag-match, not a fixer
    verdicts = issue_fixed_driver.deterministic_fixed(st, {42: "merged", 43: "open", 44: "merged"})
    assert verdicts == [{"issue": 5, "status": "fixed", "fixed_by": 42, "fixed_title": "fix",
                         "rationale": "Merged PR #42 explicitly references this issue."}]


def test_deterministic_fixed_issue_ref_rationale(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    a = st.create_issue(8, META)
    a.set_links([{"pr": 50, "how": "issue-ref", "title": "the fix"}])
    verdicts = issue_fixed_driver.deterministic_fixed(st, {50: "merged"})
    assert verdicts == [{"issue": 8, "status": "fixed", "fixed_by": 50, "fixed_title": "the fix",
                         "rationale": "This issue's text names merged PR #50 as its fix."}]


def test_candidates_orders_by_cluster_pain_then_skips_current(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    for n in (4, 5, 6):
        st.create_issue(n, META)
    hi = st.create_issue_cluster(1, "hi")
    hi.set_members([6])
    hi.set_pain(9.0)
    lo = st.create_issue_cluster(2, "lo")
    lo.set_members([4])
    lo.set_pain(1.0)
    # 5 is clusterless (pain 0). Order: 6 (9.0), 4 (1.0), 5 (0.0)
    assert issue_fixed_driver.candidates(st) == [6, 4, 5]
    # once scanned-and-current, an issue drops out
    st.edit_issue(6).record_fix_scan("not-fixed")
    assert issue_fixed_driver.candidates(st) == [4, 5]


def test_bundle_flags_live_comment_evidence(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, dict(META, author="reporter", comments=2))
    entry = issue_fixed_driver.bundle(st, only=[5])[0]
    assert entry["author"] == "reporter"
    assert entry["comments"] == 2


def test_prompt_embeds_criteria_and_placeholders():
    assert issue_fixed_driver.FIX_CRITERIA in issue_fixed_driver.FIND_FIXED_PROMPT
    assert "__BUNDLE_PATH__" in issue_fixed_driver.FIND_FIXED_PROMPT
    assert "__REPO__" not in issue_fixed_driver.FIND_FIXED_PROMPT  # substituted at module load
    assert "pre-merge behavior" in issue_fixed_driver.FIX_CRITERIA
    assert "already produced the expected behavior" in issue_fixed_driver.FIX_CRITERIA
    assert "gh issue view <n>" in issue_fixed_driver.FIND_FIXED_PROMPT
    assert "retracts the suspected cause" in issue_fixed_driver.FIND_FIXED_PROMPT


def test_apply_fixed_supersedes_needs_human(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    iss.route_to("needs-human", "judgement call")
    issue_fixed_driver.apply_verdicts(st, [
        {"issue": 5, "status": "fixed", "fixed_by": 42, "rationale": "#42 fixes it"}])
    got = st.load_issue(5)
    assert got.disposition == "close-fixed"      # a found fix outranks needs-human
    assert got.fixed_by == 42
