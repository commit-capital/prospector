from issue_triage import issue_links
from issue_triage.issue_model import Issue


def _issue(candidates, github=None):
    links = {"candidates": candidates}
    if github is not None:
        links["github"] = github
    return Issue(None, {"issue": 1, "meta": {"title": "t", "state": "open",
                                             "updated_at": "2026-09-01T00:00:00Z"},
                        "links": links})


def _link(pr, how, state="open"):
    return {"pr": pr, "how": how, "state": state, "draft": False, "title": f"PR {pr}",
            "updated_at": None, "head_sha": None}


def test_the_index_replaces_stored_direct_kinds_and_keeps_issue_owned_ones():
    iss = _issue([{"pr": 10, "how": "explicit", "title": "stale"},
                  {"pr": 11, "how": "subsystem", "title": "s"},
                  {"pr": 12, "how": "fix-found", "title": "f"}])
    got = issue_links.linked_prs(iss, [_link(20, "explicit")])
    assert [(c["pr"], c["how"]) for c in got] == [(20, "explicit"), (12, "fix-found"),
                                                  (11, "subsystem")]


def test_without_an_index_the_stored_snapshot_stands():
    iss = _issue([{"pr": 10, "how": "explicit", "title": "x"}])
    assert [c["pr"] for c in issue_links.linked_prs(iss, None)] == [10]


def test_github_closing_references_rank_with_explicit_and_strongest_kind_wins():
    iss = _issue([{"pr": 30, "how": "subsystem", "title": "s"}],
                 github=[{"pr": 30, "state": "open", "draft": False}])
    got = issue_links.linked_prs(iss, [])
    assert [(c["pr"], c["how"]) for c in got] == [(30, "github")]


def test_an_explicit_index_link_outranks_the_same_prs_github_reference():
    iss = _issue([], github=[{"pr": 20, "state": "closed", "draft": True}])
    got = issue_links.linked_prs(iss, [_link(20, "explicit")])
    assert [(c["pr"], c["how"], c["title"], c["state"], c["draft"]) for c in got] == [
        (20, "explicit", "PR 20", "open", False)]


def test_stored_candidates_are_copied_not_handed_back():
    cand = {"pr": 40, "how": "issue-ref", "title": "i"}
    got = issue_links.linked_prs(_issue([cand]), None)
    assert got[0] == {"pr": 40, "how": "issue-ref", "title": "i"}
    assert got[0] is not cand


def test_every_ranked_kind_is_one_the_pipeline_writes():
    assert set(issue_links.HOW_RANK) == {"explicit", "github", "fix-found", "issue-ref",
                                         "body-ref", "subsystem"}
    assert issue_links.REFERENCED <= set(issue_links.HOW_RANK)
    assert sorted(set(issue_links.HOW_RANK.values())) == list(range(issue_links.UNRANKED))


def test_referenced_excludes_tag_and_bare_body_matches():
    assert issue_links.referenced({"how": "github"})
    assert not issue_links.referenced({"how": "subsystem"})
    assert not issue_links.referenced({"how": "body-ref"})


def test_an_unknown_kind_ranks_after_every_known_one():
    assert issue_links.how_rank({"how": "hunch"}) > max(issue_links.HOW_RANK.values())
    assert not issue_links.referenced({"how": "hunch"})
