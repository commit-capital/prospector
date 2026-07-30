"""deep_search: coercion is pure + safe; stream judges, caches, and reports
progress. The agent subprocess (_judge_batch) is stubbed so tests are offline."""
import asyncio
import json

from prospector_app.backend import deep_search
from pipeline import model


def _collect(query, prs):
    async def go():
        return [e async for e in deep_search.stream(query, prs)]
    return asyncio.run(go())


def test_coerce_keeps_only_valid_candidate_prs():
    text = ('[{"pr": 1, "match": true, "reason": "hits the limiter"},'
            ' {"pr": 999, "match": true, "reason": "hallucinated"},'
            ' {"pr": 2, "match": false, "reason": "unrelated"}]')
    out = deep_search.coerce_judgments(text, {1, 2})
    assert out[1] == {"match": True, "reason": "hits the limiter"}
    assert out[2]["match"] is False
    assert 999 not in out                      # not in the batch → dropped


def test_coerce_handles_garbage_and_bad_shapes():
    assert deep_search.coerce_judgments("not json at all", {1}) == {}
    assert deep_search.coerce_judgments('[{"pr": "x"}, 5, {"nope": 1}]', {1}) == {}
    # bool is not a valid pr even though isinstance(True, int)
    assert deep_search.coerce_judgments('[{"pr": true, "match": true}]', {1}) == {}


def test_coerce_truncates_reason():
    text = json.dumps([{"pr": 1, "match": True, "reason": "z" * 500}])
    assert len(deep_search.coerce_judgments(text, {1})[1]["reason"]) == deep_search._REASON_BUDGET


def test_compact_record_shape(monkeypatch):
    monkeypatch.setattr(deep_search.testpaths, "cached_diff_text", lambda rec, c: None)
    rec = {"pr": 7, "meta": {"title": "Fix X", "body": "B" * 1000},
           "signals": {"greptile": 4, "ci": "passing",
                       "diffstat": {"additions": 10, "deletions": 2, "changed_files": 1}},
           "summary": {"one_liner": "fixes x", "mechanism": "does y"},
           "analysis": {"disposition": "merge"}}
    r = deep_search.compact_record(model.Pr(None, rec), "B" * 1000)
    assert r["pr"] == 7 and r["title"] == "Fix X" and r["greptile"] == 4
    assert len(r["description"]) == deep_search._BODY_BUDGET
    assert r["files"] == [] and r["disposition"] == "request-changes"  # greptile 4 < bar → derived


def test_compact_record_without_body_has_empty_description(monkeypatch):
    # The cached record carries no body; with none hydrated, description is empty.
    monkeypatch.setattr(deep_search.testpaths, "cached_diff_text", lambda rec, c: None)
    rec = {"pr": 7, "meta": {"title": "Fix X", "body": "B" * 1000}}
    r = deep_search.compact_record(model.Pr(None, rec))
    assert r["description"] == ""


def _fake_store():
    return {n: model.Pr(None, {"pr": n, "meta": {"head_sha": sha, "title": f"t{n}"}})
            for n, sha in [(1, "a"), (2, "b"), (3, "c")]}


def test_stream_judges_caches_and_reports_progress(monkeypatch, tmp_path):
    monkeypatch.setattr(deep_search, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(deep_search.data, "prs", _fake_store)
    monkeypatch.setattr(deep_search.data, "pr_bodies", lambda ns: {})
    monkeypatch.setattr(deep_search.testpaths, "cached_diff_text", lambda rec, c: None)
    calls = {"n": 0}

    async def fake_judge(query, records, sem):
        calls["n"] += 1
        return {r["pr"]: {"match": r["pr"] % 2 == 1, "reason": f"r{r['pr']}"} for r in records}
    monkeypatch.setattr(deep_search, "_judge_batch", fake_judge)

    events = _collect("works around the limiter", [1, 2, 3])
    result = next(e for e in events if e["type"] == "result")
    assert {m["pr"] for m in result["matches"]} == {1, 3}        # odd PRs match
    assert result["total"] == 3 and result["judged"] == 3 and result["from_cache"] == 0
    assert calls["n"] == 1                                       # one batch
    assert [e for e in events if e["type"] == "progress"][-1] == {"type": "progress", "done": 3, "total": 3}

    # second run over the same set: all verdicts cached, agent not called again
    calls["n"] = 0
    result2 = next(e for e in _collect("works around the limiter", [1, 2, 3]) if e["type"] == "result")
    assert {m["pr"] for m in result2["matches"]} == {1, 3}
    assert result2["from_cache"] == 3 and calls["n"] == 0


def test_stream_empty_query_short_circuits(monkeypatch):
    monkeypatch.setattr(deep_search.data, "prs", _fake_store)
    events = _collect("", [1, 2])
    assert events[-1]["type"] == "result" and events[-1]["matches"] == []


def test_stream_caps_candidate_set(monkeypatch, tmp_path):
    monkeypatch.setattr(deep_search, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(deep_search, "MAX_CANDIDATES", 2)
    monkeypatch.setattr(deep_search.data, "prs", _fake_store)
    monkeypatch.setattr(deep_search.data, "pr_bodies", lambda ns: {})
    monkeypatch.setattr(deep_search.testpaths, "cached_diff_text", lambda rec, c: None)

    async def fake_judge(query, records, sem):
        return {r["pr"]: {"match": True, "reason": "ok"} for r in records}
    monkeypatch.setattr(deep_search, "_judge_batch", fake_judge)

    result = next(e for e in _collect("anything", [1, 2, 3]) if e["type"] == "result")
    assert result["capped"] is True and result["total"] == 2   # only first 2 judged


def test_deep_search_route_streams_result(monkeypatch):
    from fastapi.testclient import TestClient
    from prospector_app.backend import app as appmod

    async def fake_stream(query, prs):
        yield {"type": "progress", "done": 0, "total": 1}
        yield {"type": "result", "matches": [{"pr": prs[0], "reason": "ok"}],
               "total": 1, "capped": False}
    monkeypatch.setattr(appmod.deep_search, "stream", fake_stream)
    c = TestClient(appmod.app)
    r = c.post("/api/prs/deep-search", json={"query": "x", "prs": [42]})
    assert r.status_code == 200
    assert "event: result" in r.text and '"pr": 42' in r.text
