"""Policy: a merge pick below the quality-gate bar (Greptile 5/5, CI passing,
mergeable, applicable) reads as request-changes with asks that close the gap.
The route is derived at read time (gates.merge_demotion) — the stored analysis
keeps ANALYZE's verdict, so a signal refresh that clears the gap restores the
merge read with nothing to re-run. The board chip derives the same way
(gates.cluster_state)."""
from pipeline import gates
from pipeline.store import Store
from pipeline.testsupport import greptile_entry, reviews_section

NOW = "2026-06-10T00:00:00+00:00"


def _pr(store, n, greptile=5, ci="passing", mergeable=True, disposition="merge"):
    rec = {"pr": n, "meta": {"title": f"t{n}", "author": "a", "state": "open",
                             "draft": False, "head_sha": "h1", "checked_at": NOW},
           "signals": {"ci": ci, "mergeable": mergeable,
                       "checked_at": NOW, "against_head_sha": "h1"},
           "reviews": reviews_section("h1", NOW, greptile=greptile_entry(greptile, "h1")),
           "drift": {"state": "applicable" if mergeable else "conflicts",
                     "checked_at": NOW, "against_head_sha": "h1"},
           "analysis": {"disposition": disposition, "rationale": "best",
                        "checked_at": NOW, "against_head_sha": "h1"}}
    store.save_pr(rec)


def _cluster(store, cid, prs, outcome="merge-ready"):
    store.save_cluster({"id": cid, "root_problem": "x", "prs": [],
                        "outcome": outcome, "checked_at": NOW})
    store.edit_cluster(cid).set_members(prs)   # wires cluster.prs AND each pr.cluster_ids
    return store.load_cluster(cid)


def _state(store, cid):
    return gates.cluster_state(store.load_cluster(cid), store.all_prs())


def test_straddler_below_bar_flips_every_cluster_it_belongs_to(tmp_path):
    # PR 1 straddles clusters 5 and 9 (#196). Below the bar it reads
    # request-changes, so each cluster's derived state is awaiting-authors.
    s = Store(tmp_path)
    _pr(s, 1, greptile=4)
    _cluster(s, 5, [1])
    _cluster(s, 9, [1])
    assert s.load_pr(1).disposition == "request-changes"
    assert _state(s, 5) == "awaiting-authors"
    assert _state(s, 9) == "awaiting-authors"


def test_clean_merge_reads_merge(tmp_path):
    s = Store(tmp_path)
    _pr(s, 1, greptile=5)
    _cluster(s, 1, [1])
    assert s.load_pr(1).disposition == "merge"


def test_greptile_4_reads_request_changes_with_ask(tmp_path):
    s = Store(tmp_path)
    _pr(s, 1, greptile=4)
    _cluster(s, 1, [1])
    pr = s.load_pr(1)
    assert pr.disposition == "request-changes"
    assert any("Greptile" in ask and "5/5" in ask for ask in pr.asks)
    # the cluster has no merge-reading member → awaiting-authors
    assert _state(s, 1) == "awaiting-authors"


def test_conflicts_read_rebase_ask(tmp_path):
    s = Store(tmp_path)
    _pr(s, 1, greptile=5, mergeable=False)
    _cluster(s, 1, [1])
    pr = s.load_pr(1)
    assert pr.disposition == "request-changes"
    assert any("ebase" in ask for ask in pr.asks)


def test_cluster_stays_on_the_merge_path_if_another_merge_clean(tmp_path):
    s = Store(tmp_path)
    _pr(s, 1, greptile=4)         # reads request-changes
    _pr(s, 2, greptile=5)         # reads merge
    _cluster(s, 1, [1, 2])
    assert s.load_pr(2).disposition == "merge"
    # still merge-routed (awaiting its security review), not awaiting-authors
    assert _state(s, 1) == "security-pending"


def test_existing_asks_are_extended_not_clobbered(tmp_path):
    s = Store(tmp_path)
    _pr(s, 1, greptile=4, disposition="merge")
    rec = s.load_pr(1)
    rec.raw["analysis"]["asks"] = ["pre-existing ask"]
    s.save_pr(rec)
    asks = s.load_pr(1).asks
    assert asks[0] == "pre-existing ask" and len(asks) >= 2


def test_overridden_red_security_survives(tmp_path):
    s = Store(tmp_path)
    _pr(s, 1, greptile=5)
    rec = s.load_pr(1)
    rec.raw["security"] = {"verdict": "RED", "findings": [],
                           "override": {"reason": "false positive", "by": "tester", "date": NOW},
                           "checked_at": NOW, "against_head_sha": "h1"}
    s.save_pr(rec)
    assert s.load_pr(1).disposition == "merge"


def test_signal_refresh_restores_the_merge_read(tmp_path):
    # The bar is derived, never stored: a Greptile score that reaches the bar on
    # a later refresh restores the merge read in place.
    s = Store(tmp_path)
    _pr(s, 1, greptile=4)
    assert s.load_pr(1).disposition == "request-changes"
    rec = s.load_pr(1)
    rec.raw["reviews"]["greptile"]["score"] = 5
    s.save_pr(rec)
    assert s.load_pr(1).disposition == "merge"
    assert s.load_pr(1).asks is None
