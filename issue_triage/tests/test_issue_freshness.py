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


def _analyzed(*, updated: str, content: str | None, stamp: str | None) -> issue_model.Issue:
    meta = {"title": "t", "state": "open", "updated_at": updated}
    if content is not None:
        meta["content_updated_at"] = content
    return _issue({"issue": 1, "meta": meta,
                   "analysis": {"disposition": "needs-human", "against_updated_at": stamp}})


def test_a_bot_label_or_comment_after_analysis_keeps_it_current():
    # The bot's write moved updated_at past the stamp; the content time stayed put.
    iss = _analyzed(updated="2026-10-01T12:00:00Z", content="2026-10-01T09:00:00Z",
                    stamp="2026-10-01T10:00:00Z")
    assert issue_freshness.is_current(iss, "analysis")


def test_a_material_change_after_analysis_makes_it_stale():
    # A person's comment or a body edit moved the content time past the stamp.
    iss = _analyzed(updated="2026-10-01T12:00:00Z", content="2026-10-01T11:00:00Z",
                    stamp="2026-10-01T10:00:00Z")
    assert not issue_freshness.is_current(iss, "analysis")


def test_a_stamp_at_the_content_time_is_current():
    iss = _analyzed(updated="2026-10-01T12:00:00Z", content="2026-10-01T10:00:00Z",
                    stamp="2026-10-01T10:00:00Z")
    assert issue_freshness.is_current(iss, "analysis")


def test_an_issue_without_a_content_time_is_held_to_the_exact_stamp():
    moved = _analyzed(updated="2026-10-01T12:00:00Z", content=None,
                      stamp="2026-10-01T10:00:00Z")
    assert not issue_freshness.is_current(moved, "analysis")
    same = _analyzed(updated="2026-10-01T12:00:00Z", content=None,
                     stamp="2026-10-01T12:00:00Z")
    assert issue_freshness.is_current(same, "analysis")


def test_a_missing_or_unreadable_stamp_is_stale_against_a_content_time():
    for stamp in (None, "", "not a time"):
        iss = _analyzed(updated="2026-10-01T12:00:00Z", content="2026-10-01T09:00:00Z",
                        stamp=stamp)
        assert not issue_freshness.is_current(iss, "analysis")


def test_a_content_time_does_not_excuse_the_max_age_window():
    iss = _issue({"issue": 1,
                  "meta": {"title": "t", "state": "open", "updated_at": "2026-10-01T12:00:00Z",
                           "content_updated_at": "2026-09-01T00:00:00Z"},
                  "fix_scan": {"status": "not-fixed", "checked_at": "2026-09-02T00:00:00Z",
                               "against_updated_at": "2026-09-02T00:00:00Z"}})
    assert issue_freshness.is_current(iss, "fix_scan", max_age_days=30, today="2026-09-20")
    assert not issue_freshness.is_current(iss, "fix_scan", max_age_days=30, today="2026-10-20")
