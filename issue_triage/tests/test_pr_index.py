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
    assert idx[1][0]["draft"] is False


def test_a_link_carries_only_the_fields_the_accessor_reads():
    idx = pr_index.build([_pr(10, [{"issue": 1, "how": "explicit"}])])
    assert set(idx[1][0]) == {"pr", "how", "state", "draft", "title"}


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


def test_from_store_matches_build_over_the_records_without_reading_them(tmp_path, monkeypatch):
    """The store-backed index is the same one `build` makes from the records,
    read as five projected fields of the open and merged PRs."""
    from pipeline.store import Store
    st = Store(tmp_path)
    for n, state, draft, linked in (
            (10, "open", True, [{"issue": 1, "how": "explicit"}, {"issue": 2, "how": "subsystem"}]),
            (11, "merged", False, [{"issue": 1, "how": "body-ref"}, {"issue": 3, "how": "explicit"}]),
            (12, "closed", False, [{"issue": 1, "how": "explicit"}]),
            (13, "open", False, None)):
        rec = {"pr": n, "meta": {"title": f"PR {n}", "state": state, "head_sha": f"h{n}",
                                 "checked_at": "t", "draft": draft}}
        if linked is not None:
            rec["issues"] = {"linked": linked, "checked_at": "t", "against_head_sha": f"h{n}"}
        st.save_pr(rec)
    expected = pr_index.build(st.all_prs().values())
    monkeypatch.setattr("pipeline.store.Store", lambda *a, **k: st)
    monkeypatch.setattr(type(st), "all_prs", lambda self: (_ for _ in ()).throw(
        AssertionError("from_store read the PR records")))
    assert pr_index.from_store() == expected
    assert expected[1][0] == {"pr": 10, "how": "explicit", "state": "open", "draft": True,
                              "title": "PR 10"}
