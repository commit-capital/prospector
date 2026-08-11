# prospector_app/backend/tests/test_query_row_cache.py
"""query_prs's per-snapshot row cache (#76): rows are reused across queries while
the snapshot stands still, dropped when it moves, and never allowed to serve a
stale response/ack signal (those are overlaid fresh on every query)."""
from pipeline import model
from prospector_app.backend import data
from prospector_app.backend import responses
from prospector_app.backend import service


def _rec(pr: int, state: str = "open") -> model.Pr:
    return model.Pr(None, {
        "pr": pr, "meta": {"title": f"t{pr}", "author": "al", "state": state,
                           "draft": False, "head_sha": f"h{pr}", "url": "u",
                           "created_at": "2026-01-01T00:00:00+00:00",
                           "updated_at": "2026-01-01T00:00:00+00:00"},
        "signals": {"greptile": 5, "ci": "passing", "mergeable": True,
                    "diffstat": {"additions": 1, "deletions": 0, "changed_files": 1}},
        "drift": {"state": "applicable"}})


def _count_pr_row_builds(monkeypatch) -> dict[str, int]:
    calls = {"n": 0}
    real = service.pr_row

    def counting(n, rec=None):
        calls["n"] += 1
        return real(n, rec)

    monkeypatch.setattr(service, "pr_row", counting)
    return calls


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


def test_generation_bump_rebuilds_rows(monkeypatch):
    snap = {1: _rec(1)}
    monkeypatch.setattr(data, "prs", lambda: snap)
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    monkeypatch.setattr(data, "author_stats", lambda handle: None)
    calls = _count_pr_row_builds(monkeypatch)

    service.query_prs({})
    assert calls["n"] == 1
    monkeypatch.setattr(data, "_generation", data.generation() + 1)
    service.query_prs({})
    assert calls["n"] == 2


def test_new_snapshot_object_rebuilds_rows(monkeypatch):
    # A caller that swaps the snapshot dict without touching the generation
    # (how tests monkeypatch data.prs) still gets fresh rows: the cache is
    # keyed on the dict's identity too.
    monkeypatch.setattr(data, "prs", lambda: {1: _rec(1)})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    monkeypatch.setattr(data, "author_stats", lambda handle: None)
    calls = _count_pr_row_builds(monkeypatch)

    service.query_prs({})
    service.query_prs({})
    assert calls["n"] == 2  # a fresh dict per call → no reuse


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
