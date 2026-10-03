# prospector_app/backend/tests/test_query_row_cache.py
"""query_prs's row cache (#76): a row is rebuilt only when something it reads
moved — its own record, a cluster it is or was in, a PR it shares a cluster
with, its cached diff, or one of the global inputs (settings, profile, active
reviewers, the hour) — and never serves a stale response/ack signal, claim, or
author stat (those are overlaid fresh on every query)."""
import dataclasses
import subprocess
import threading
import time

from pipeline import model
from pipeline import profile
from pipeline import review_policy
from pipeline import storekit
from prospector_app.backend import data
from prospector_app.backend import responses
from prospector_app.backend import service


def _rec(pr: int, state: str = "open", ci: str = "passing", head: str | None = None,
         author: str = "al") -> model.Pr:
    return model.Pr(None, {
        "pr": pr, "meta": {"title": f"t{pr}", "author": author, "state": state,
                           "draft": False, "head_sha": head or f"h{pr}", "url": "u",
                           "created_at": "2026-01-01T00:00:00+00:00",
                           "updated_at": "2026-01-01T00:00:00+00:00"},
        "signals": {"greptile": 5, "ci": ci, "mergeable": True,
                    "diffstat": {"additions": 1, "deletions": 0, "changed_files": 1}},
        "drift": {"state": "applicable"}})


def _cluster(cid: int, prs: list[int]) -> model.Cluster:
    return model.Cluster(None, {"id": cid, "root_problem": f"c{cid}", "prs": list(prs)})


def _count_pr_row_builds(monkeypatch) -> dict[str, int]:
    calls = {"n": 0}
    real = service.pr_row

    def counting(n, rec=None):
        calls["n"] += 1
        return real(n, rec)

    monkeypatch.setattr(service, "pr_row", counting)
    return calls


def _record_builds(monkeypatch, delay: float = 0.0) -> list[int]:
    built: list[int] = []
    real = service.pr_row

    def recording(n, rec=None):
        built.append(int(n))
        if delay:
            time.sleep(delay)
        return real(n, rec)

    monkeypatch.setattr(service, "pr_row", recording)
    return built


def _cached_rows() -> dict[int, dict]:
    assert service._ROWS is not None
    return dict(service._ROWS.rows)


def _items(out: dict) -> dict[int, dict]:
    return {r["number"]: r for r in out["items"]}


class _StubStore:
    """Store stub for _freshen: yields a scripted sequence of deltas."""

    def __init__(self, batches):
        self._batches = list(batches)

    def _next(self):
        return self._batches.pop(0) if self._batches else ({}, [], None)

    def prs_since(self, watermark):
        prs, _deleted, hi = self._next()
        return prs, hi

    def clusters_since(self, watermark):
        return self._next()

    def load_claims(self):
        return {}


def _serve(monkeypatch, *batches) -> None:
    """Load a scripted store into data's snapshot and serve it to readers with
    no background freshen consuming the rest of the script: each later
    `data._freshen()` publishes the next PR delta and cluster delta."""
    monkeypatch.setattr(data, "_store", _StubStore(batches))
    monkeypatch.setattr(data, "author_stats", lambda handle: None)
    data._freshen(full=True)
    monkeypatch.setattr(data, "_loaded", True)
    monkeypatch.setattr(data, "_last_check", float("inf"))


def test_rows_reused_while_snapshot_stands_still(monkeypatch):
    snap = {1: _rec(1), 2: _rec(2)}
    monkeypatch.setattr(data, "prs", lambda: snap)
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    monkeypatch.setattr(data, "author_stats", lambda handle: None)
    calls = _count_pr_row_builds(monkeypatch)

    first = service.query_prs({})
    assert first["total"] == 2 and calls["n"] == 2
    second = service.query_prs({"ci": "passing"})
    assert second["total"] == 2 and calls["n"] == 2  # no rebuild on the second query


def test_freshen_touching_one_pr_rebuilds_only_that_row(monkeypatch):
    _serve(monkeypatch,
           ({1: _rec(1), 2: _rec(2), 3: _rec(3)}, [], "t1"), ({}, [], None),
           ({2: _rec(2, ci="failing")}, [], "t2"), ({}, [], None))
    built = _record_builds(monkeypatch)
    service.query_prs({})
    before = _cached_rows()

    data._freshen()
    out = service.query_prs({})
    after = _cached_rows()

    assert built == [1, 2, 3, 2]
    assert after[1] is before[1] and after[3] is before[3]
    assert after[2] is not before[2]
    assert _items(out)[2]["signals"]["ci"] == "failing"


def test_freshen_touching_a_clustered_pr_rebuilds_its_cluster_peers(monkeypatch):
    # A row's suggestion may name the open merge picks it shares a cluster with,
    # so a change to one member reaches every other member's row.
    _serve(monkeypatch,
           ({1: _rec(1), 2: _rec(2), 3: _rec(3)}, [], "t1"), ({9: _cluster(9, [1, 2])}, [], "c1"),
           ({1: _rec(1, ci="failing")}, [], "t2"), ({}, [], None))
    built = _record_builds(monkeypatch)
    service.query_prs({})
    before = _cached_rows()

    data._freshen()
    service.query_prs({})

    assert sorted(built[3:]) == [1, 2]
    assert _cached_rows()[3] is before[3]


def test_cluster_membership_change_rebuilds_old_and_new_members(monkeypatch):
    _serve(monkeypatch,
           ({n: _rec(n) for n in (1, 2, 3, 4)}, [], "t1"), ({9: _cluster(9, [1, 2])}, [], "c1"),
           ({}, [], None), ({9: _cluster(9, [2, 3])}, [], "c2"))
    built = _record_builds(monkeypatch)
    service.query_prs({})
    before = _cached_rows()

    data._freshen()
    out = service.query_prs({})

    assert sorted(built[4:]) == [1, 2, 3]
    assert _cached_rows()[4] is before[4]
    assert _items(out)[1]["clusters"] == [] and _items(out)[3]["clusters"] == [9]


def test_deleted_cluster_rebuilds_its_members(monkeypatch):
    _serve(monkeypatch,
           ({n: _rec(n) for n in (1, 2, 3)}, [], "t1"), ({9: _cluster(9, [1, 2])}, [], "c1"),
           ({}, [], None), ({}, [9], "c2"))
    built = _record_builds(monkeypatch)
    service.query_prs({})

    data._freshen()
    out = service.query_prs({})

    assert sorted(built[3:]) == [1, 2]
    assert _items(out)[1]["clusters"] == [] and _items(out)[2]["clusters"] == []


def test_refresh_that_finds_nothing_keeps_every_row(monkeypatch):
    # An explicit refresh advances the generation whether or not a record
    # moved; the rows follow the records, not the counter.
    _serve(monkeypatch, ({1: _rec(1), 2: _rec(2)}, [], "t1"), ({}, [], None))
    built = _record_builds(monkeypatch)
    service.query_prs({})
    gen = data.generation()

    data.refresh()
    service.query_prs({})

    assert data.generation() > gen
    assert built == [1, 2]


def test_settings_change_rebuilds_every_row(monkeypatch):
    _serve(monkeypatch, ({1: _rec(1), 2: _rec(2)}, [], "t1"), ({}, [], None))
    built = _record_builds(monkeypatch)
    service.query_prs({})

    monkeypatch.setenv("TRIAGE_VERIFY_MAX_AGE_DAYS", "7")
    service.query_prs({})

    assert built == [1, 2, 1, 2]


def test_profile_change_rebuilds_every_row(monkeypatch):
    _serve(monkeypatch, ({1: _rec(1), 2: _rec(2)}, [], "t1"), ({}, [], None))
    built = _record_builds(monkeypatch)
    assert not any(r["trusted_author"] for r in service.query_prs({})["items"])

    trusting = dataclasses.replace(profile.active(), trusted_authors=("al",))
    monkeypatch.setattr(profile, "active", lambda: trusting)
    out = service.query_prs({})

    assert built == [1, 2, 1, 2]
    assert all(r["trusted_author"] for r in out["items"])


def test_active_reviewer_change_rebuilds_every_row(monkeypatch):
    # In auto mode the active set follows the reviewers registry, which moves
    # without any PR record moving.
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "auto")
    monkeypatch.setattr(review_policy, "_seen_cache",
                        (time.monotonic(), {"superagent": {"last_observed_at": storekit.now()}}))
    _serve(monkeypatch, ({1: _rec(1), 2: _rec(2)}, [], "t1"), ({}, [], None))
    built = _record_builds(monkeypatch)
    assert all("superagent" in r["reviews"] for r in service.query_prs({})["items"])

    monkeypatch.setattr(review_policy, "_seen_cache", (time.monotonic(), {}))
    out = service.query_prs({})

    assert built == [1, 2, 1, 2]
    assert not any("superagent" in r["reviews"] for r in out["items"])


def test_hour_change_rebuilds_every_row(monkeypatch):
    # Freshness windows count days and the hunters' retry cooldowns count
    # hours, so a row is held at most until the UTC hour turns.
    _serve(monkeypatch, ({1: _rec(1), 2: _rec(2)}, [], "t1"), ({}, [], None))
    built = _record_builds(monkeypatch)
    monkeypatch.setattr(service, "_row_hour", lambda: "2026-10-03T10")
    service.query_prs({})
    service.query_prs({})

    monkeypatch.setattr(service, "_row_hour", lambda: "2026-10-03T11")
    service.query_prs({})

    assert built == [1, 2, 1, 2]


def test_new_snapshot_object_rebuilds_rows(monkeypatch):
    # A caller that swaps the snapshot for new records without touching the
    # generation (how tests monkeypatch data.prs) still gets fresh rows.
    monkeypatch.setattr(data, "prs", lambda: {1: _rec(1)})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    monkeypatch.setattr(data, "author_stats", lambda handle: None)
    calls = _count_pr_row_builds(monkeypatch)

    service.query_prs({})
    service.query_prs({})
    assert calls["n"] == 2  # a fresh record per call → no reuse


def test_author_stats_overlay_stays_live_on_cached_rows(monkeypatch):
    _serve(monkeypatch, ({1: _rec(1)}, [], "t1"), ({}, [], None))
    stats = {"al": {"handle": "al", "merged": 1}}
    monkeypatch.setattr(data, "author_stats", lambda handle: stats.get(handle or ""))
    built = _record_builds(monkeypatch)
    assert service.query_prs({})["items"][0]["author_stats"]["merged"] == 1

    stats["al"] = {"handle": "al", "merged": 2}

    assert service.query_prs({})["items"][0]["author_stats"]["merged"] == 2
    assert built == [1]


def test_caching_a_diff_rebuilds_that_prs_row(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "DIFF_CACHE", tmp_path / "app")
    monkeypatch.setattr(service, "PIPELINE_DIFF_CACHE", tmp_path / "pipeline")
    monkeypatch.setattr(service, "_FACETS", {})
    _serve(monkeypatch, ({1: _rec(1, head="d1"), 2: _rec(2, head="d2")}, [], "t1"), ({}, [], None))
    built = _record_builds(monkeypatch)
    assert service.query_prs({})["items"][0]["changed_paths"] == []
    before = _cached_rows()

    diff = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    monkeypatch.setattr(service, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=diff, stderr=""))
    service.get_diff(1)
    out = service.query_prs({})

    assert built == [1, 2, 1]
    assert _cached_rows()[2] is before[2]
    assert _items(out)[1]["changed_paths"] == ["src/a.py"]


def test_concurrent_queries_build_each_row_once(monkeypatch):
    _serve(monkeypatch, ({n: _rec(n) for n in (1, 2, 3)}, [], "t1"), ({}, [], None))
    built = _record_builds(monkeypatch, delay=0.02)
    start = threading.Barrier(2)

    def query() -> None:
        start.wait()
        service.query_prs({})

    threads = [threading.Thread(target=query) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(built) == [1, 2, 3]


def test_responses_overlay_stays_live_on_cached_rows(monkeypatch):
    snap = {42: _rec(42, state="closed")}
    monkeypatch.setattr(data, "prs", lambda: snap)
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    monkeypatch.setattr(data, "author_stats", lambda handle: None)
    sig = {"pr": 42, "reopened": False, "new_commits": False, "replied": True,
           "resubmitted": False, "resubmitted_pr": None,
           "last_response_at": "2026-02-01T00:00:00+00:00", "snippet": "why?",
           "by": "al", "ack": None}
    live: dict[int, dict] = {42: sig}
    monkeypatch.setattr(responses, "for_pr", lambda n: live.get(int(n)))

    out = service.query_prs({"responses": "replied"})
    assert out["total"] == 1 and out["items"][0]["responses"]["ack"] is None

    # An ack lands between two queries over the same snapshot: the cached row
    # must not shadow it — the PR leaves the responses queue immediately.
    live[42] = {**sig, "ack": {"at": "2026-02-02T00:00:00+00:00", "by": "op"}}
    assert service.query_prs({"responses": "replied"})["total"] == 0


def test_freshen_keeps_identity_and_generation_when_nothing_changed(monkeypatch):
    # data._prs / data._generation are read directly: data.prs() runs _ensure,
    # whose own full load would consume the stub's scripted batches.
    monkeypatch.setattr(data, "_store", _StubStore([
        ({1: _rec(1)}, [], "t1"), ({}, [], "t1"),   # load: PR delta, cluster delta
        ({}, [], "t1"), ({}, [], "t1"),             # second freshen: no changes
    ]))
    data._freshen(full=True)
    snap, gen = data._prs, data.generation()
    assert gen == 1 and set(snap) == {1}
    data._freshen()
    assert data._prs is snap  # unchanged content → the same dict object
    assert data.generation() == gen


def test_freshen_bumps_generation_on_a_delta(monkeypatch):
    monkeypatch.setattr(data, "_store", _StubStore([
        ({1: _rec(1)}, [], "t1"), ({}, [], "t1"),
        ({2: _rec(2)}, [], "t2"), ({}, [], "t1"),
    ]))
    data._freshen(full=True)
    snap, gen = data._prs, data.generation()
    data._freshen()
    assert set(data._prs) == {1, 2}
    assert data._prs is not snap
    assert data.generation() == gen + 1


def test_freshen_keeps_the_record_objects_it_did_not_refetch(monkeypatch):
    # The row cache tells a changed PR from an unchanged one by its record's
    # identity, so a delta must leave every other record object in place.
    monkeypatch.setattr(data, "_store", _StubStore([
        ({1: _rec(1), 2: _rec(2)}, [], "t1"), ({}, [], "t1"),
        ({2: _rec(2, ci="failing")}, [], "t2"), ({}, [], "t1"),
    ]))
    data._freshen(full=True)
    one, two = data._prs[1], data._prs[2]
    data._freshen()
    assert data._prs[1] is one and data._prs[2] is not two
