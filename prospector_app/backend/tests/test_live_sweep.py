"""Live sweep: third-party PR-state drift (open/closed/merged + mergeable) fetched
from GitHub and persisted into the shared store, so every operator converges on
GitHub's truth without a per-machine overlay. The GraphQL fetch (live_states) is
monkeypatched; these cover the persistence, targeting, and staleness logic."""
from datetime import datetime, timedelta, timezone

from pipeline import gates
from pipeline.store import Store
from prospector_app.backend import data
from prospector_app.backend import freshness_live
from prospector_app.backend import service

HEAD = "abc123"


def _raw(n, state="open", mergeable=True):
    return {"pr": n, "meta": {"title": f"PR {n}", "author": "a", "state": state, "draft": False,
                              "head_sha": HEAD, "url": "x", "created_at": "2026-06-10T00:00:00+00:00",
                              "updated_at": "2026-06-10T00:00:00+00:00",
                              "checked_at": "2026-06-10T00:00:00+00:00"},
            "signals": {"greptile": 5, "ci": "passing", "mergeable": mergeable,
                        "checked_at": "2026-06-10T00:00:00+00:00", "against_head_sha": HEAD},
            "drift": {"state": "applicable", "checked_at": "2026-06-10T00:00:00+00:00",
                      "against_head_sha": HEAD}}


def _wire(tmp_path, monkeypatch, seed, live):
    """A real store seeded with `seed` records, wired into the data snapshot, with
    live_states returning `live`."""
    st = Store(tmp_path)
    for rec in seed:
        st.save_pr(rec)
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setattr(freshness_live, "live_states", lambda prs: (live, set()))
    data.refresh()
    return st


def test_sweep_persists_closed_upstream_to_store(tmp_path, monkeypatch):
    st = _wire(tmp_path, monkeypatch, [_raw(5, "open")],
               {5: {"state": "closed", "merged": False, "head": HEAD, "mergeable": "UNKNOWN"}})
    res = freshness_live.sweep()
    assert res["changed"] == 1 and res["prs"] == [5]
    assert st.load_pr(5).state == "closed"   # persisted to the shared store


def test_sweep_normalizes_merged(tmp_path, monkeypatch):
    st = _wire(tmp_path, monkeypatch, [_raw(6, "open")],
               {6: {"state": "closed", "merged": True, "head": HEAD, "mergeable": "UNKNOWN"}})
    freshness_live.sweep()
    assert st.load_pr(6).state == "merged"


def test_sweep_persists_conflict_into_signals(tmp_path, monkeypatch):
    st = _wire(tmp_path, monkeypatch, [_raw(5, "open", mergeable=True)],
               {5: {"state": "open", "merged": False, "head": HEAD, "mergeable": "CONFLICTING"}})
    freshness_live.sweep()
    reloaded = st.load_pr(5)
    assert reloaded.mergeable is False                       # conflict persisted
    assert reloaded.section("signals")["against_head_sha"] == HEAD  # freshness stamp kept


def test_sweep_change_count_ignores_already_matching(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, [_raw(5, "open"), _raw(7, "closed")],
          {5: {"state": "open", "merged": False, "head": HEAD, "mergeable": "MERGEABLE"},
           7: {"state": "closed", "merged": False, "head": HEAD, "mergeable": "UNKNOWN"}})
    # 5 targeted (open); 7 not (already closed). Neither diverges → no writes.
    res = freshness_live.sweep()
    assert res["checked"] == 1 and res["changed"] == 0


def test_unknown_mergeable_does_not_clobber_committed(tmp_path, monkeypatch):
    st = _wire(tmp_path, monkeypatch, [_raw(5, "open", mergeable=True)],
               {5: {"state": "open", "merged": False, "head": HEAD, "mergeable": "UNKNOWN"}})
    freshness_live.sweep()
    assert st.load_pr(5).mergeable is True   # UNKNOWN left the committed value alone


def test_persist_live_skips_never_ingested_pr(tmp_path, monkeypatch):
    st = _wire(tmp_path, monkeypatch, [], {})
    changed = freshness_live.persist_live(
        {99: {"state": "closed", "merged": False, "head": HEAD}}, data.prs())
    assert changed == []
    assert st.load_pr(99) is None


def test_sweep_stamps_last_swept_at(tmp_path, monkeypatch):
    st = _wire(tmp_path, monkeypatch, [_raw(5, "open")],
               {5: {"state": "open", "merged": False, "head": HEAD, "mergeable": "MERGEABLE"}})
    assert freshness_live.last_swept_at() is None
    res = freshness_live.sweep()
    assert res["fetched_at"] is not None
    assert st.load_live_sweep()["swept_at"] == res["fetched_at"]
    assert freshness_live.last_swept_at() == res["fetched_at"]


def test_sweep_retries_missing_prs_before_marking_complete(tmp_path, monkeypatch):
    st = _wire(tmp_path, monkeypatch, [_raw(5), _raw(6)], {})
    calls: list[list[int]] = []

    def fetch(prs):
        calls.append(prs)
        if len(calls) == 1:
            return ({5: {"state": "open", "merged": False, "head": HEAD,
                         "mergeable": "MERGEABLE"}}, set())
        return ({6: {"state": "open", "merged": False, "head": HEAD,
                     "mergeable": "MERGEABLE"}}, set())

    monkeypatch.setattr(freshness_live, "live_states", fetch)
    res = freshness_live.sweep()

    assert calls == [[5, 6], [6]]
    assert res["attempted"] == 2 and res["checked"] == 2
    assert res["complete"] is True and res["failed"] == []
    assert st.load_live_sweep()["swept_at"] == res["fetched_at"]


def test_partial_sweep_does_not_advance_completion_marker(tmp_path, monkeypatch):
    st = _wire(tmp_path, monkeypatch, [_raw(5), _raw(6)], {})
    prior = "2026-06-01T00:00:00+00:00"
    st.save_live_sweep({"swept_at": prior})
    monkeypatch.setattr(
        freshness_live, "live_states",
        lambda prs: ({5: {"state": "open", "merged": False, "head": HEAD,
                          "mergeable": "MERGEABLE"}} if 5 in prs else {}, set()))

    res = freshness_live.sweep()

    assert res["attempted"] == 2 and res["checked"] == 1
    assert res["complete"] is False and res["failed"] == [6]
    assert st.load_live_sweep()["swept_at"] == prior


def test_targeted_sweep_does_not_mark_full_corpus_complete(tmp_path, monkeypatch):
    st = _wire(tmp_path, monkeypatch, [_raw(5), _raw(6)],
               {5: {"state": "open", "merged": False, "head": HEAD,
                    "mergeable": "MERGEABLE"}})
    prior = "2026-06-01T00:00:00+00:00"
    st.save_live_sweep({"swept_at": prior})

    res = freshness_live.sweep([5])

    assert res["complete"] is True and res["checked"] == 1
    assert st.load_live_sweep()["swept_at"] == prior


def test_stale_reflects_the_shared_singleton(tmp_path, monkeypatch):
    st = _wire(tmp_path, monkeypatch, [], {})
    assert freshness_live.stale(60) is True          # never swept → worth a sweep
    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    st.save_live_sweep({"swept_at": recent})
    assert freshness_live.stale(60) is False         # 5 min old, TTL 60 → reuse
    old = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat(timespec="seconds")
    st.save_live_sweep({"swept_at": old})
    assert freshness_live.stale(60) is True           # 90 min old → sweep


def test_swept_closed_pr_reads_closed_and_blocks_gate(tmp_path, monkeypatch):
    """After a sweep persists a closed-upstream state, the snapshot reads it and the
    merge gate refuses (proves the store-persistence replaces the read-time overlay)."""
    st = _wire(tmp_path, monkeypatch, [_raw(5, "open")],
               {5: {"state": "closed", "merged": False, "head": HEAD, "mergeable": "UNKNOWN"}})
    assert gates.pr_clean(st.load_pr(5))[0] is True   # clean while open
    freshness_live.sweep()
    data.refresh()
    swept = data.prs()[5]
    assert swept.state == "closed"
    ok, reasons = gates.pr_clean(swept)
    assert not ok and any("not open" in r for r in reasons)


def test_sweep_rechecks_closed_by_us_and_persists_reopen(tmp_path, monkeypatch):
    """A PR we closed is off the store-open set, but the sweep still re-checks the
    closed-by-us set — so an upstream reopen lands back in the store as open."""
    from prospector_app.backend import activity
    st = _wire(tmp_path, monkeypatch, [_raw(8, "closed")],
               {8: {"state": "open", "merged": False, "head": HEAD, "mergeable": "MERGEABLE"}})
    monkeypatch.setattr(activity, "closed_by_us_prs", lambda: [8])
    res = freshness_live.sweep()
    assert 8 in res["prs"]
    assert st.load_pr(8).state == "open"   # upstream reopen persisted, not just an overlay


def test_swept_closed_pr_drops_from_open_queue(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, [_raw(5, "open")],
          {5: {"state": "closed", "merged": False, "head": HEAD, "mergeable": "UNKNOWN"}})
    freshness_live.sweep()
    data.refresh()
    assert service.query_prs({})["total"] == 0   # closed → out of the open queue


def test_sweep_persists_diffstat_and_has_tests(tmp_path, monkeypatch):
    st = _wire(tmp_path, monkeypatch, [_raw(5, "open")],
               {5: {"state": "open", "merged": False, "head": HEAD, "mergeable": "MERGEABLE",
                    "diffstat": {"additions": 12, "deletions": 3, "changed_files": 4},
                    "has_tests": True}})
    res = freshness_live.sweep()
    sig = st.load_pr(5).section("signals")
    assert sig["diffstat"] == {"additions": 12, "deletions": 3, "changed_files": 4}
    assert sig["has_tests"] is True
    assert res["changed"] == 1 and res["prs"] == [5]  # diffstat-only change still counts


def test_sweep_persists_new_ci_verdict(tmp_path, monkeypatch):
    seed = _raw(5, "open")
    seed["signals"].pop("ci")
    st = _wire(tmp_path, monkeypatch, [seed],
               {5: {"state": "open", "merged": False, "head": HEAD,
                    "mergeable": "MERGEABLE", "ci": "passing"}})

    res = freshness_live.sweep()

    assert st.load_pr(5).ci == "passing"
    assert res["changed"] == 1 and res["prs"] == [5]


def test_sweep_preserves_ci_when_live_rollup_is_missing(tmp_path, monkeypatch):
    st = _wire(tmp_path, monkeypatch, [_raw(5, "open")],
               {5: {"state": "open", "merged": False, "head": HEAD,
                    "mergeable": "MERGEABLE", "ci": None}})

    freshness_live.sweep()

    assert st.load_pr(5).ci == "passing"


def test_sweep_does_not_stamp_signals_from_a_moved_head(tmp_path, monkeypatch):
    seed = _raw(5, "open")
    st = _wire(tmp_path, monkeypatch, [seed],
               {5: {"state": "open", "merged": False, "head": "new-head",
                    "mergeable": "CONFLICTING", "ci": "failing",
                    "diffstat": {"additions": 99, "deletions": 1, "changed_files": 4},
                    "has_tests": True}})

    res = freshness_live.sweep()

    rec = st.load_pr(5)
    assert rec.ci == "passing"
    assert rec.mergeable is True
    assert rec.signals.get("diffstat") is None
    assert res["changed"] == 0


def test_sweep_retires_scrubbed_pr(tmp_path, monkeypatch):
    """A number GitHub reports NOT_FOUND is gone upstream: the sweep closes it,
    marks it unresolvable, counts it handled (not failed), and still completes."""
    st = _wire(tmp_path, monkeypatch, [_raw(5, "open")], {})
    monkeypatch.setattr(freshness_live, "live_states", lambda prs: ({}, {5}))
    res = freshness_live.sweep()
    rec = st.load_pr(5)
    assert rec.state == "closed"
    assert rec.unresolvable is True
    assert res["retired"] == [5] and res["failed"] == []
    assert res["complete"] is True
    assert st.load_live_sweep()["swept_at"] == res["fetched_at"]


def test_sweep_retires_scrubbed_closed_by_us_pr_without_reopening(tmp_path, monkeypatch):
    """A closed-by-us PR that got scrubbed keeps its closed state and gains the
    unresolvable mark, so the reopen re-check stops targeting it."""
    from prospector_app.backend import activity
    st = _wire(tmp_path, monkeypatch, [_raw(8, "closed")], {})
    monkeypatch.setattr(activity, "closed_by_us_prs", lambda: [8])
    monkeypatch.setattr(freshness_live, "live_states", lambda prs: ({}, set(prs)))
    res = freshness_live.sweep()
    rec = st.load_pr(8)
    assert rec.state == "closed" and rec.unresolvable is True
    assert res["retired"] == [8] and res["complete"] is True


def test_sweep_excludes_retired_prs_from_targets(tmp_path, monkeypatch):
    """An unresolvable PR is off both target sets — the open set and the
    closed-by-us reopen re-check — so later sweeps never re-query it."""
    from prospector_app.backend import activity
    gone = _raw(8, "closed")
    gone["meta"]["unresolvable"] = True
    _wire(tmp_path, monkeypatch, [gone, _raw(5, "open")],
          {5: {"state": "open", "merged": False, "head": HEAD, "mergeable": "MERGEABLE"}})
    calls: list[list[int]] = []
    monkeypatch.setattr(
        freshness_live, "live_states",
        lambda prs: (calls.append(list(prs)) or (
            {5: {"state": "open", "merged": False, "head": HEAD,
                 "mergeable": "MERGEABLE"}}, set())))
    monkeypatch.setattr(activity, "closed_by_us_prs", lambda: [8])
    res = freshness_live.sweep()
    assert calls and all(8 not in c for c in calls)
    assert res["complete"] is True and res["failed"] == []


def test_sweep_skips_write_when_diffstat_unchanged(tmp_path, monkeypatch):
    seed = _raw(5, "open")
    seed["signals"]["diffstat"] = {"additions": 12, "deletions": 3, "changed_files": 4}
    seed["signals"]["has_tests"] = True
    _wire(tmp_path, monkeypatch, [seed],
          {5: {"state": "open", "merged": False, "head": HEAD, "mergeable": "MERGEABLE",
               "diffstat": {"additions": 12, "deletions": 3, "changed_files": 4},
               "has_tests": True}})
    res = freshness_live.sweep()
    assert res["changed"] == 0   # nothing diverged → no write
