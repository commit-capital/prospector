"""authors.author_stats: per-author leaderboard = live store group-by folded with
the historical baseline. Pure aggregation — no gh, no store."""
from pipeline import authors
from pipeline import model
from pipeline.store import Store


def _pr(n, author, state):
    return model.Pr(None, {"pr": n, "meta": {"title": "t", "author": author,
                                             "state": state, "head_sha": "h", "url": "u",
                                             "comments": 2}})


def test_live_only_author():
    prs = {1: _pr(1, "al", "open"), 2: _pr(2, "al", "merged"), 3: _pr(3, "al", "closed")}
    row = authors.author_stats({}, prs)["al"]
    assert (row["total"], row["open"], row["merged"], row["closed_unmerged"]) == (3, 1, 1, 1)
    assert row["comments"] == 6
    assert row["merge_rate"] == 0.5          # 1 merged / 2 decided
    assert row["url"] == "https://github.com/al"


def test_baseline_only_author():
    baseline = {"bo": {"handle": "Bo", "total": 4, "open": 0, "merged": 3,
                       "closed_unmerged": 1, "comments": 9}}
    row = authors.author_stats(baseline, {})["bo"]
    assert (row["merged"], row["closed_unmerged"], row["total"]) == (3, 1, 4)
    assert row["handle"] == "Bo"


def test_author_in_both_is_summed():
    prs = {1: _pr(1, "al", "merged")}
    baseline = {"al": {"handle": "al", "total": 10, "open": 0, "merged": 5,
                       "closed_unmerged": 5, "comments": 0}}
    row = authors.author_stats(baseline, prs)["al"]
    assert row["merged"] == 6          # 1 live + 5 baseline
    assert row["closed_unmerged"] == 5
    assert row["total"] == 11


def test_merge_rate_none_when_no_decided_prs():
    prs = {1: _pr(1, "al", "open")}
    row = authors.author_stats({}, prs)["al"]
    assert row["merge_rate"] is None
    # shrunk rate holds at the base rate for a genuine unknown
    assert abs(row["merge_rate_shrunk"] - 0.12) < 1e-9


def test_shrunk_rate_orders_rejection_streak_below_unknown():
    # 0 merged of 8 decided sinks below the base rate; an unknown holds at it.
    rejected = authors.author_stats({"x": {"handle": "x", "total": 8, "open": 0,
                                           "merged": 0, "closed_unmerged": 8, "comments": 0}}, {})["x"]
    assert rejected["merge_rate_shrunk"] < 0.12


def test_null_author_pr_skipped():
    prs = {1: _pr(1, None, "open")}
    assert authors.author_stats({}, prs) == {}


def test_case_collision_merges_and_live_casing_wins():
    # Live "Al" and baseline "al" collapse to one row; counts sum and the live
    # display casing wins (the live loop locks the display handle first).
    prs = {1: _pr(1, "Al", "merged")}
    baseline = {"al": {"handle": "al", "total": 2, "open": 0, "merged": 1,
                       "closed_unmerged": 1, "comments": 0}}
    table = authors.author_stats(baseline, prs)
    assert list(table) == ["al"]
    row = table["al"]
    assert row["handle"] == "Al"
    assert row["url"] == "https://github.com/Al"
    assert (row["total"], row["merged"], row["closed_unmerged"]) == (3, 2, 1)


def test_empty_inputs():
    assert authors.author_stats({}, {}) == {}
    assert authors.author_stats(None, {}) == {}


def _page(nodes, has_next=False, cursor=None):
    return {"data": {"repository": {"pullRequests": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": nodes}}}}


def _node(number, login, state, comments=0):
    author = {"login": login} if login else None
    return {"number": number, "state": state, "author": author,
            "comments": {"totalCount": comments}}


def test_capture_excludes_store_present_and_paginates(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.save_pr({"pr": 100, "meta": {"title": "t", "author": "al", "state": "open",
                                       "head_sha": "h", "url": "u"}})
    pages = [
        _page([_node(100, "al", "MERGED"), _node(1, "al", "MERGED")], has_next=True, cursor="C1"),
        _page([_node(2, "al", "CLOSED"), _node(3, "Bo", "MERGED", comments=4), _node(4, None, "CLOSED")]),
    ]
    calls = iter(pages)
    monkeypatch.setattr(authors.gh, "gh_graphql", lambda q, **k: next(calls))

    reg = authors.capture_baseline(store)
    a = reg["authors"]
    # PR 100 is in the store -> excluded from the baseline
    assert a["al"] == {"handle": "al", "total": 2, "open": 0, "merged": 1,
                       "closed_unmerged": 1, "comments": 0}
    assert a["bo"]["merged"] == 1 and a["bo"]["comments"] == 4
    assert "author" not in a  # null-author node skipped
    assert reg["materialized_at"] is not None
    assert store.load_author_baseline()["authors"]["al"]["merged"] == 1


def test_capture_stops_on_failed_page(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(authors.gh, "gh_graphql", lambda q, **k: None)
    reg = authors.capture_baseline(store)
    assert reg["authors"] == {}
