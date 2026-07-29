# review_cockpit/backend/test_bulk.py
"""bulk orchestration: run_bulk() applies one shared action to many PRs;
run_cluster() applies each PR's own disposition (the cluster page's mixed
'execute all'). Both loop the existing per-PR executor and yield one SSE event
per PR + a summary. The executor (and run_cluster's training.capture) is
monkeypatched — bulk adds no new write path, it only orchestrates, so we assert
routing + summary, not GitHub calls."""
import asyncio
import json

from review_cockpit.backend import bulk
from review_cockpit.backend import executor
from review_cockpit.backend import models


def _drain(gen):
    async def go():
        return [ev async for ev in gen]
    return asyncio.run(go())


def test_close_routes_to_execute_pr_with_shared_comment(monkeypatch):
    seen = []
    def fake_exec(n, action, *, token, dry_run):
        seen.append((n, action.action, action.comment))
        return {"pr": n, "action": action.action, "status": "dry-run", "detail": "ok"}
    monkeypatch.setattr(executor, "execute_pr", fake_exec)
    monkeypatch.setattr(executor, "mint_bot_token", lambda: None)

    evs = _drain(bulk.run_bulk([1, 2], "CLOSE_DUP", comment="dup of #9",
                               canonical=9, dry_run=True))
    results = [e for e in evs if e["event"] == "result"]
    assert len(results) == 2
    assert seen[0] == (1, "CLOSE_DUP", "dup of #9")
    done = [e for e in evs if e["event"] == "done"][0]
    summary = done_summary(done)
    assert summary["dry-run"] == 2  # executor's status string is hyphenated


def test_per_pr_comments_override_shared(monkeypatch):
    """#182: each PR can carry its own suggested comment; a PR missing from the
    map falls back to the shared `comment`."""
    seen = {}
    def fake_exec(n, action, *, token, dry_run):
        seen[n] = action.comment
        return {"pr": n, "action": action.action, "status": "dry-run", "detail": "ok"}
    monkeypatch.setattr(executor, "execute_pr", fake_exec)
    monkeypatch.setattr(executor, "mint_bot_token", lambda: None)

    _drain(bulk.run_bulk([1, 2, 3], "CLOSE_DUP", comment="shared",
                         comments={1: "dup of #11", 2: "dup of #22"}, dry_run=True))
    assert seen[1] == "dup of #11"   # its own comment
    assert seen[2] == "dup of #22"   # its own comment
    assert seen[3] == "shared"       # not in the map → shared fallback


def test_merge_routes_to_merge_pr_and_reports_blocked(monkeypatch):
    def fake_merge(n, method, *, dry_run, reason=None):
        return {"pr": n, "action": "MERGE",
                "status": "blocked" if n == 2 else "merged", "detail": "gate"}
    monkeypatch.setattr(executor, "merge_pr", fake_merge)
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "tok")
    evs = _drain(bulk.run_bulk([1, 2], "MERGE", dry_run=False))
    done = [e for e in evs if e["event"] == "done"][0]
    s = done_summary(done)
    assert s["merged"] == 1 and s["blocked"] == 1


def test_cap_rejects_oversized_batch(monkeypatch):
    evs = _drain(bulk.run_bulk(list(range(1, 1002)), "CLOSE", dry_run=True))
    err = [e for e in evs if e["event"] == "error"]
    assert err and "cap" in err[0]["data"].lower()


def done_summary(ev):
    import json
    return json.loads(ev["data"])["summary"]


def test_bulk_route_streams(monkeypatch):
    from fastapi.testclient import TestClient
    from review_cockpit.backend import app as appmod
    monkeypatch.setattr(appmod.executor, "execute_pr",
                        lambda n, a, *, token, dry_run: {"pr": n, "action": a.action,
                                                         "status": "dry-run", "detail": "ok"})
    monkeypatch.setattr(appmod.executor, "mint_bot_token", lambda: None)
    c = TestClient(appmod.app)
    r = c.post("/api/execute/bulk",
               json={"prs": [1, 2], "action": "CLOSE", "dry_run": True})
    assert r.status_code == 200
    assert "result" in r.text and "done" in r.text


def test_run_cluster_routes_each_disposition(monkeypatch):
    monkeypatch.setattr(executor, "merge_pr",
                        lambda n, method, *, dry_run, reason=None: {"pr": n, "action": "MERGE", "status": "dry-run"})
    monkeypatch.setattr(executor, "submit_review",
                        lambda n, ev, body, *, token, dry_run: {"pr": n, "action": f"REVIEW:{ev}", "status": "dry-run"})
    monkeypatch.setattr(executor, "execute_pr",
                        lambda n, a, *, token, dry_run: {"pr": n, "action": a.action, "status": "dry-run"})
    monkeypatch.setattr(executor, "mint_bot_token", lambda: None)
    monkeypatch.setattr(bulk.training, "capture", lambda *a, **k: None)
    items = [
        models.ClusterItem(pr=1, action="merge", method="squash"),
        models.ClusterItem(pr=2, action="request-changes", comment="please fix"),
        models.ClusterItem(pr=3, action="close-dup", canonical=9, comment="dup of #9"),
    ]
    evs = _drain(bulk.run_cluster(items, dry_run=True))
    results = [json.loads(e["data"]) for e in evs if e["event"] == "result"]
    assert [r["action"] for r in results] == ["MERGE", "REVIEW:request-changes", "CLOSE_DUP"]
    assert done_summary([e for e in evs if e["event"] == "done"][0])["dry-run"] == 3


def test_run_cluster_unknown_disposition_is_skipped(monkeypatch):
    monkeypatch.setattr(bulk.training, "capture", lambda *a, **k: None)
    monkeypatch.setattr(executor, "mint_bot_token", lambda: None)
    evs = _drain(bulk.run_cluster([models.ClusterItem(pr=1, action="needs-human")], dry_run=True))
    [res] = [json.loads(e["data"]) for e in evs if e["event"] == "result"]
    assert res["status"] == "skipped"


def test_run_cluster_cap_rejects_oversized_batch():
    evs = _drain(bulk.run_cluster([models.ClusterItem(pr=n, action="close") for n in range(1, 1002)], dry_run=True))
    err = [e for e in evs if e["event"] == "error"]
    assert err and "cap" in err[0]["data"].lower()


def test_run_cluster_skips_closes_when_a_merge_fails(monkeypatch):
    # #188: a failed merge must skip the dependent closes (don't orphan-close).
    closed = []
    monkeypatch.setattr(executor, "merge_pr",
                        lambda n, method, *, dry_run, reason=None: {"pr": n, "action": "MERGE", "status": "error", "detail": "boom"})
    monkeypatch.setattr(executor, "execute_pr",
                        lambda n, a, *, token, dry_run: closed.append(n) or {"pr": n, "action": a.action, "status": "dry-run"})
    monkeypatch.setattr(executor, "mint_bot_token", lambda: None)
    monkeypatch.setattr(bulk.training, "capture", lambda *a, **k: None)
    items = [models.ClusterItem(pr=1, action="merge"), models.ClusterItem(pr=2, action="close-dup", canonical=1)]
    evs = _drain(bulk.run_cluster(items, dry_run=False))
    assert closed == []  # the close never ran
    results = [json.loads(e["data"]) for e in evs if e["event"] == "result"]
    assert [r["action"] for r in results] == ["MERGE"]  # only the failed merge attempt
    done = json.loads([e for e in evs if e["event"] == "done"][0]["data"])
    assert "skipped 1 follow-up action" in done.get("aborted", "")


def test_run_cluster_runs_closes_when_merge_succeeds(monkeypatch):
    closed = []
    monkeypatch.setattr(executor, "merge_pr",
                        lambda n, method, *, dry_run, reason=None: {"pr": n, "action": "MERGE", "status": "merged"})
    monkeypatch.setattr(executor, "execute_pr",
                        lambda n, a, *, token, dry_run: closed.append(n) or {"pr": n, "action": a.action, "status": "closed"})
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "tok")
    monkeypatch.setattr(bulk.training, "capture", lambda *a, **k: None)
    items = [models.ClusterItem(pr=2, action="close-dup", canonical=1), models.ClusterItem(pr=1, action="merge")]
    evs = _drain(bulk.run_cluster(items, dry_run=False))
    assert closed == [2]  # merge ran first (despite being listed second), then the close
    results = [json.loads(e["data"]) for e in evs if e["event"] == "result"]
    assert [r["action"] for r in results] == ["MERGE", "CLOSE_DUP"]
    done = json.loads([e for e in evs if e["event"] == "done"][0]["data"])
    assert "aborted" not in done


def test_cluster_route_streams(monkeypatch):
    from fastapi.testclient import TestClient
    from review_cockpit.backend import app as appmod
    monkeypatch.setattr(appmod.executor, "execute_pr",
                        lambda n, a, *, token, dry_run: {"pr": n, "action": a.action,
                                                         "status": "dry-run", "detail": "ok"})
    monkeypatch.setattr(appmod.executor, "mint_bot_token", lambda: None)
    monkeypatch.setattr(appmod.bulk.training, "capture", lambda *a, **k: None)
    c = TestClient(appmod.app)
    r = c.post("/api/execute/cluster",
               json={"items": [{"pr": 1, "action": "close-dup", "canonical": 9}], "dry_run": True})
    assert r.status_code == 200
    assert "result" in r.text and "done" in r.text
