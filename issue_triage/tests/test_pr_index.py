from pipeline.model import Pr
from issue_triage import pr_index


def _pr(n, linked, state="open", draft=False):
    return Pr(None, {"pr": n, "meta": {"title": f"PR {n}", "state": state, "draft": draft,
                                       "head_sha": f"h{n}", "updated_at": "2026-09-01T00:00:00Z"},
                     "issues": {"linked": linked}})


def test_build_inverts_direct_links_only():
    idx = pr_index.build([
        _pr(10, [{"issue": 1, "how": "explicit"}, {"issue": 2, "how": "subsystem"}]),
        _pr(11, [{"issue": 1, "how": "body-ref"}], state="merged"),
    ])
    assert sorted(idx) == [1]
    assert [(link["pr"], link["how"], link["state"]) for link in idx[1]] == [
        (10, "explicit", "open"), (11, "body-ref", "merged")]
    assert idx[1][0]["head_sha"] == "h10" and idx[1][0]["draft"] is False


def test_build_keeps_open_and_merged_prs_only():
    linked = [{"issue": 1, "how": "explicit"}]
    idx = pr_index.build([_pr(10, linked), _pr(11, linked, state="merged"),
                          _pr(12, linked, state="closed"), _pr(13, linked, state=None)])
    assert [link["pr"] for link in idx[1]] == [10, 11]


def test_an_issue_only_closed_prs_link_is_absent():
    assert pr_index.build([_pr(12, [{"issue": 1, "how": "explicit"}], state="closed")]) == {}


def test_explicit_beats_body_ref_for_the_same_pr_and_issue():
    idx = pr_index.build([_pr(10, [{"issue": 1, "how": "body-ref"},
                                   {"issue": 1, "how": "explicit"}])])
    assert [link["how"] for link in idx[1]] == ["explicit"]
