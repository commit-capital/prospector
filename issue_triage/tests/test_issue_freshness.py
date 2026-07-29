"""issue_freshness: against_updated_at staleness, mirroring the PR head_sha check."""
from issue_triage import issue_freshness
from issue_triage import issue_model


def _issue(rec: dict) -> issue_model.Issue:
    return issue_model.Issue(None, rec)


def test_analysis_current_when_token_matches():
    iss = _issue({
        "issue": 1,
        "meta": {"title": "t", "state": "open", "updated_at": "T2"},
        "analysis": {"disposition": "needs-human", "against_updated_at": "T2"},
    })
    assert issue_freshness.is_current(iss, "analysis")


def test_analysis_stale_when_issue_updated():
    iss = _issue({
        "issue": 1,
        "meta": {"title": "t", "state": "open", "updated_at": "T3"},
        "analysis": {"disposition": "needs-human", "against_updated_at": "T2"},
    })
    assert not issue_freshness.is_current(iss, "analysis")


def test_missing_section_not_current():
    iss = _issue({"issue": 1, "meta": {"title": "t", "state": "open", "updated_at": "T"}})
    assert not issue_freshness.is_current(iss, "analysis")


def test_links_section_not_token_bound():
    iss = _issue({
        "issue": 1,
        "meta": {"title": "t", "state": "open", "updated_at": "T9"},
        "links": {"candidates": []},
    })
    # links is not in UPDATED_BOUND, so it is current regardless of updated_at
    assert issue_freshness.is_current(iss, "links")
