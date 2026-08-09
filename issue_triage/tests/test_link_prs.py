from issue_triage import link_prs
from issue_triage.link_prs import candidate_prs, parse_body_issue_refs, parse_issue_refs


def test_candidate_prs_caps_subsystem_matches_keeps_all_direct():
    """Every explicit/issue-ref match is kept; weak subsystem matches are capped at
    SUBSYSTEM_CAP so a broad subsystem can't bloat the stored list."""
    cap = link_prs.SUBSYSTEM_CAP
    prs = ([{"number": 1, "body": "Fixes #5", "title": "x", "subsystem": "auth"}]  # explicit
           + [{"number": 100 + i, "body": "", "title": "s", "subsystem": "auth", "state": "open"}
              for i in range(cap + 5)])                                            # subsystem flood
    cands = candidate_prs(issue_number=5, issue_subsystem="auth", prs=prs)
    assert [c["pr"] for c in cands if c["how"] != "subsystem"] == [1]
    subsystem = [c for c in cands if c["how"] == "subsystem"]
    assert len(subsystem) == cap
    assert [c["pr"] for c in subsystem] == list(range(100, 100 + cap))  # first N, order kept


def test_parse_fixes_closes_resolves():
    body = "This Fixes #123 and also closes: #45.\nResolves #6789."
    assert parse_issue_refs(body) == {45, 123, 6789}


def test_parse_ignores_non_keyword_hash():
    assert parse_issue_refs("see #999 for context") == set()


def test_broad_body_refs_include_prose_issue_reference():
    body = "Refs upstream issue #3846; see #4000 for context."
    assert parse_body_issue_refs(body) == {3846, 4000}


def test_candidate_prs_marks_non_closing_body_reference_lower_confidence():
    prs = [{"number": 10, "body": "Refs upstream issue #3846", "title": "route fix"}]
    assert candidate_prs(3846, None, prs) == [
        {"pr": 10, "how": "body-ref", "title": "route fix"}]


def test_candidate_prs_matches_explicit_link():
    prs = [{"number": 10, "body": "Fixes #4242", "title": "lock fix",
            "subsystem": "execution-locks"}]
    cands = candidate_prs(issue_number=4242, issue_subsystem="execution-locks", prs=prs)
    assert any(c["pr"] == 10 and c["how"] == "explicit" for c in cands)


def test_explicit_ref_matches_merged_pr():
    """A merged PR whose body says Fixes #N stays a candidate — durable evidence
    the issue is likely fixed."""
    prs = [{"number": 700, "title": "landed fix", "body": "Fixes #42", "state": "merged"}]
    cands = candidate_prs(issue_number=42, issue_subsystem="auth", prs=prs)
    assert cands == [{"pr": 700, "how": "explicit", "title": "landed fix"}]


def test_subsystem_match_skips_non_open_prs():
    """A merged or closed PR that merely shares the subsystem is not a candidate —
    tag-match evidence only means anything for PRs that could still land."""
    prs = [{"number": 700, "title": "t", "body": "", "state": "merged", "subsystem": "auth"},
           {"number": 701, "title": "t", "body": "", "state": "closed", "subsystem": "auth"},
           {"number": 702, "title": "t", "body": "", "state": "open", "subsystem": "auth"}]
    cands = candidate_prs(issue_number=42, issue_subsystem="auth", prs=prs)
    assert [c["pr"] for c in cands] == [702]


def test_issue_text_pr_refs_link_as_issue_ref():
    """PR references in the issue's own text ("PR #626", a pull URL, "fixed
    by #627") become issue-ref candidates, validated against the PR corpus."""
    prs = [{"number": 626, "title": "a", "body": "", "state": "merged"},
           {"number": 627, "title": "b", "body": "", "state": "open"},
           {"number": 628, "title": "c", "body": "", "state": "open"}]
    text = ("Still broken despite fix in PR #626.\n"
            "See https://github.com/test-owner/test-repo/pull/628 too; "
            "supposedly fixed by #627 and by #9999.")
    cands = candidate_prs(issue_number=42, issue_subsystem=None, prs=prs, issue_text=text)
    assert {(c["pr"], c["how"]) for c in cands} == {
        (626, "issue-ref"), (627, "issue-ref"), (628, "issue-ref")}


def test_bare_number_refs_are_not_issue_refs():
    """A bare '#N' in issue text is usually another issue — it never links."""
    prs = [{"number": 626, "title": "a", "body": "", "state": "open"}]
    cands = candidate_prs(issue_number=42, issue_subsystem=None, prs=prs,
                          issue_text="related to #626 maybe")
    assert cands == []


def test_explicit_outranks_issue_ref_for_same_pr():
    prs = [{"number": 626, "title": "a", "body": "Fixes #42", "state": "open"}]
    cands = candidate_prs(issue_number=42, issue_subsystem=None, prs=prs,
                          issue_text="fixed by PR #626")
    assert cands == [{"pr": 626, "how": "explicit", "title": "a"}]
