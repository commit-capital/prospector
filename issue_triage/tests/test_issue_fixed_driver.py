"""Find-fixed driver: candidate selection (pain order), bundle, verdict application."""
from issue_triage import issue_fixed_driver, issue_store

META = {"title": "t", "body": "b", "state": "open", "updated_at": "T1"}
CAUSAL_EVIDENCE = {
    "reported_origin": "src/service.ts:produceState",
    "fix_hunk": "src/service.ts:produceState",
    "relationship": "The hunk changes the reported producer directly.",
    "before": "The issue inputs produced the invalid state.",
    "after": "The issue inputs leave the state unchanged.",
    "current_path_check": "The producer no longer writes the invalid state.",
}


def test_apply_fixed_verdict_marks_close_fixed(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, META)
    n = issue_fixed_driver.apply_verdicts(st, [
        {"issue": 5, "status": "fixed", "fixed_by": 42, "upstream_date": "2026-05-01",
         "gist": "Crash on null.", "rationale": "#42 adds the guard", "fixed_title": "guard",
         "causal_evidence": CAUSAL_EVIDENCE}])
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
        {"issue": 5, "status": "fixed", "fixed_by": 42, "rationale": "#42 fixes it",
         "causal_evidence": CAUSAL_EVIDENCE}])
    got = st.load_issue(5)
    assert got.disposition == "close-dup"        # close-dup outranks close-fixed
    assert got.fix_scan["status"] == "fixed"     # evidence still recorded


def test_apply_fixed_sets_close_fixed_when_unranked_or_lower(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, META)                      # no disposition
    b = st.create_issue(6, META)
    b.route_to("request-repro", "need info")     # keep-open, lower than a close
    issue_fixed_driver.apply_verdicts(st, [
        {"issue": 5, "status": "fixed", "fixed_by": 42, "rationale": "r",
         "causal_evidence": CAUSAL_EVIDENCE},
        {"issue": 6, "status": "fixed", "fixed_by": 43, "rationale": "r",
         "causal_evidence": CAUSAL_EVIDENCE}])
    assert st.load_issue(5).disposition == "close-fixed"
    assert st.load_issue(6).disposition == "close-fixed"  # close-fixed beats request-repro


def test_apply_fixed_without_fixed_by_raises(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, META)
    try:
        issue_fixed_driver.apply_verdicts(st, [
            {"issue": 5, "status": "fixed", "rationale": "r",
             "causal_evidence": CAUSAL_EVIDENCE}])
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_apply_fixed_without_complete_causal_evidence_raises(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, META)
    verdict = {"issue": 5, "status": "fixed", "fixed_by": 42, "rationale": "r",
               "causal_evidence": dict(CAUSAL_EVIDENCE, current_path_check="")}
    try:
        issue_fixed_driver.apply_verdicts(st, [verdict])
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "current_path_check" in str(exc)


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
    assert "change to another producer of the same symptom" in issue_fixed_driver.FIX_CRITERIA
    assert "current_path_check" in issue_fixed_driver.FIND_FIXED_PROMPT
    assert "gh issue view <n>" in issue_fixed_driver.FIND_FIXED_PROMPT
    assert "retracts the suspected cause" in issue_fixed_driver.FIND_FIXED_PROMPT


def test_apply_fixed_supersedes_needs_human(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    iss.route_to("needs-human", "judgement call")
    issue_fixed_driver.apply_verdicts(st, [
        {"issue": 5, "status": "fixed", "fixed_by": 42, "rationale": "#42 fixes it",
         "causal_evidence": CAUSAL_EVIDENCE}])
    got = st.load_issue(5)
    assert got.disposition == "close-fixed"      # a found fix outranks needs-human
    assert got.fixed_by == 42
