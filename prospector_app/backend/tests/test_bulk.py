"""bulk orchestration: run_bulk() applies one shared action to many PRs;
run_cluster() applies each PR's own disposition (the cluster page's mixed
'execute all'). Both stream one SSE result per PR and a summary. bulk adds no
write path of its own, so the executor's public functions are faked and the
tests hold what bulk itself decides: per-PR comments, the summary and its
warnings, the batch cap, merges before their dependent closes, and an event
loop left free while the executor runs."""
import asyncio
import json
import time

import pytest

from prospector_app.backend import bulk
from prospector_app.backend import executor
from prospector_app.backend import models


def _drain(gen):
    async def go():
        return [ev async for ev in gen]
    return asyncio.run(go())



def test_per_pr_comments_override_shared(monkeypatch):
    """#182: each PR can carry its own suggested comment; a PR missing from the
    map falls back to the shared `comment`."""
    seen = {}
    def fake_exec(n, action, *, dry_run):
        seen[n] = action.comment
        return {"pr": n, "action": action.action, "status": "dry-run", "detail": "ok"}
    monkeypatch.setattr(executor, "execute_pr", fake_exec)
    monkeypatch.setattr("pipeline.bot_token.get", lambda: None)

    _drain(bulk.run_bulk([1, 2, 3], "CLOSE_DUP", comment="shared",
                         comments={1: "dup of #11", 2: "dup of #22"}, dry_run=True))
    assert seen[1] == "dup of #11"   # its own comment
    assert seen[2] == "dup of #22"   # its own comment
    assert seen[3] == "shared"       # not in the map → shared fallback



def test_a_bulk_merge_goes_through_the_gated_merge(monkeypatch):
    monkeypatch.setattr(executor, "merge_pr", lambda n, method, *, dry_run, reason=None: {
        "pr": n, "action": "MERGE", "status": "blocked" if n == 2 else "merged"})
    monkeypatch.setattr("pipeline.bot_token.get", lambda: "tok")
    evs = _drain(bulk.run_bulk([1, 2], "MERGE", dry_run=False))
    assert done_summary([e for e in evs if e["event"] == "done"][0]) == {
        "merged": 1, "blocked": 1}


def test_review_retrigger_routes_and_schedules_refresh(monkeypatch):
    baselines = {1: object(), 2: object()}
    seen = []
    scheduled = []
    monkeypatch.setattr("pipeline.bot_token.get", lambda: "tok")
    monkeypatch.setattr(bulk.review_refresh, "capture", lambda n, rid: baselines[n])
    monkeypatch.setattr(executor, "retrigger_review",
                        lambda n, rid, *, dry_run: seen.append((n, rid, dry_run)) or
                        {"pr": n, "action": "REVIEW_RETRIGGER", "status": "executed"})
    monkeypatch.setattr(bulk.review_refresh, "schedule",
                        lambda n, rid, baseline: scheduled.append((n, rid, baseline)))

    evs = _drain(bulk.run_bulk([1, 2], "REVIEW_RETRIGGER", dry_run=False))

    # No reviewer named: the first active review reviewer with a mention (Greptile
    # under the pinned profile) is the one re-triggered.
    assert seen == [(1, "greptile", False), (2, "greptile", False)]
    assert scheduled == [(1, "greptile", baselines[1]), (2, "greptile", baselines[2])]
    done = [e for e in evs if e["event"] == "done"][0]
    assert done_summary(done) == {"executed": 2}


def test_review_retrigger_dry_run_does_not_capture_or_schedule(monkeypatch):
    monkeypatch.setattr("pipeline.bot_token.get",
                        lambda: (_ for _ in ()).throw(AssertionError("must not mint")))
    monkeypatch.setattr(bulk.review_refresh, "capture",
                        lambda n, rid: (_ for _ in ()).throw(AssertionError("must not capture")))
    monkeypatch.setattr(bulk.review_refresh, "schedule",
                        lambda *args: (_ for _ in ()).throw(AssertionError("must not schedule")))
    seen = []
    monkeypatch.setattr(executor, "retrigger_review",
                        lambda n, rid, *, dry_run: seen.append((n, rid, dry_run)) or
                        {"pr": n, "action": "REVIEW_RETRIGGER", "status": "dry-run"})

    _drain(bulk.run_bulk([1], "REVIEW_RETRIGGER", reviewer="coderabbit", dry_run=True))

    assert seen == [(1, "coderabbit", True)]


def test_queue_verify_routes_to_the_verify_queue_without_a_token(monkeypatch):
    # Queueing for verification is a local store write: no bot token is fetched
    # even on a live run, and a pre-check refusal (already in flight, closed,
    # threat-flagged) reads as skipped, not error.
    monkeypatch.setattr("pipeline.bot_token.get",
                        lambda: (_ for _ in ()).throw(AssertionError("must not mint")))
    queued = []
    def fake_queue(n, source=None):
        if n == 2:
            raise ValueError(f"PR #{n} already has a queued verification request")
        queued.append(n)
        return {"pr": n, "status": "queued"}
    monkeypatch.setattr(bulk.verify_queue, "queue_pr", fake_queue)

    evs = _drain(bulk.run_bulk([1, 2, 3], "QUEUE_VERIFY", dry_run=False))

    assert queued == [1, 3]
    results = [json.loads(e["data"]) for e in evs if e["event"] == "result"]
    assert [r["status"] for r in results] == ["queued", "skipped", "queued"]
    assert "already has" in results[1]["detail"]
    done = [e for e in evs if e["event"] == "done"][0]
    assert done_summary(done) == {"queued": 2, "skipped": 1}


def test_queue_verify_queues_in_dry_run_mode(monkeypatch):
    # The dry/live posting mode gates upstream writes; the verify queue is a
    # local store write, so queueing proceeds in either mode.
    queued = []
    monkeypatch.setattr(bulk.verify_queue, "queue_pr",
                        lambda n, source=None: queued.append(n) or {"pr": n, "status": "queued"})
    evs = _drain(bulk.run_bulk([7], "QUEUE_VERIFY", dry_run=True))
    assert queued == [7]
    assert done_summary([e for e in evs if e["event"] == "done"][0]) == {"queued": 1}


def test_run_security_schedules_background_jobs_in_the_per_pr_dispatch(monkeypatch):
    jobs_started = []
    scheduled = []

    def fake_start(kind, cluster=None, pr=None, count=None):
        assert kind == "security-review"
        if pr == 2:
            raise ValueError("a security-review job for PR 2 is already running")
        job = {"id": pr + 100, "pr": pr}
        jobs_started.append(job)
        return job

    monkeypatch.setattr(bulk.jobs, "start_job", fake_start)
    monkeypatch.setattr(bulk.jobs, "schedule_job", scheduled.append)
    monkeypatch.setattr("pipeline.bot_token.get",
                        lambda: (_ for _ in ()).throw(AssertionError("must not mint")))

    evs = _drain(bulk.run_bulk([1, 2, 3], "RUN_SECURITY", dry_run=False))

    results = [json.loads(e["data"]) for e in evs if e["event"] == "result"]
    assert [r["status"] for r in results] == ["queued", "skipped", "queued"]
    assert [r.get("job_id") for r in results] == [101, None, 103]
    assert scheduled == jobs_started
    assert done_summary([e for e in evs if e["event"] == "done"][0]) == {
        "queued": 2, "skipped": 1}




@pytest.mark.parametrize("run", [
    lambda: bulk.run_bulk(list(range(1, 1002)), "CLOSE", dry_run=True),
    lambda: bulk.run_cluster([models.ClusterItem(pr=n, action="close") for n in range(1, 1002)],
                             dry_run=True),
], ids=["bulk", "cluster"])
def test_cap_rejects_oversized_batch(run):
    err = [e for e in _drain(run()) if e["event"] == "error"]
    assert err and "cap" in err[0]["data"].lower()


def done_summary(ev):
    import json
    return json.loads(ev["data"])["summary"]


def test_bulk_route_streams(monkeypatch):
    from fastapi.testclient import TestClient
    from prospector_app.backend import app as appmod
    monkeypatch.setattr(appmod.executor, "execute_pr",
                        lambda n, a, *, dry_run: {"pr": n, "action": a.action,
                                                         "status": "dry-run", "detail": "ok"})
    monkeypatch.setattr("pipeline.bot_token.get", lambda: None)
    c = TestClient(appmod.app)
    r = c.post("/api/execute/bulk",
               json={"prs": [1, 2], "action": "CLOSE", "dry_run": True})
    assert r.status_code == 200
    assert "result" in r.text and "done" in r.text


def test_bulk_done_frame_warns_on_bookkeeping_errors(monkeypatch):
    """A PR that executed upstream but whose bookkeeping failed counts as
    executed in the summary, and the done frame carries a warning naming the
    failure — so a batch-wide deterministic store failure is visible once,
    loudly, instead of read off 39 identical per-row details."""
    def fake_exec(n, action, *, dry_run):
        return {"pr": n, "action": action.action, "status": "executed",
                "detail": "commented + closed; bookkeeping failed",
                "bookkeeping_error": "StaleSchemaError: store schema is v9 but this code is v8"}
    monkeypatch.setattr(executor, "execute_pr", fake_exec)
    monkeypatch.setattr("pipeline.bot_token.get", lambda: "tok")

    evs = _drain(bulk.run_bulk([1, 2, 3], "CLOSE_STALE", dry_run=False))
    done = json.loads([e for e in evs if e["event"] == "done"][0]["data"])
    assert done["summary"]["executed"] == 3
    assert done["bookkeeping_errors"] == 3
    assert "StaleSchemaError" in done["warning"]



def test_run_cluster_done_frame_warns_on_bookkeeping_errors(monkeypatch):
    monkeypatch.setattr(executor, "execute_pr",
                        lambda n, a, *, dry_run: {"pr": n, "action": a.action, "status": "executed",
                                                         "bookkeeping_error": "StaleSchemaError: v9 vs v8"})
    monkeypatch.setattr("pipeline.bot_token.get", lambda: "tok")
    monkeypatch.setattr(bulk.training, "capture", lambda *a, **k: None)
    evs = _drain(bulk.run_cluster([models.ClusterItem(pr=1, action="close-dup", canonical=9)],
                                  dry_run=False))
    done = json.loads([e for e in evs if e["event"] == "done"][0]["data"])
    assert done["bookkeeping_errors"] == 1
    assert "StaleSchemaError" in done["warning"]


def test_run_cluster_routes_each_disposition(monkeypatch):
    monkeypatch.setattr(executor, "merge_pr",
                        lambda n, method, *, dry_run, reason=None: {"pr": n, "action": "MERGE", "status": "dry-run"})
    monkeypatch.setattr(executor, "submit_review",
                        lambda n, ev, body, *, dry_run: {"pr": n, "action": f"REVIEW:{ev}", "status": "dry-run"})
    monkeypatch.setattr(executor, "execute_pr",
                        lambda n, a, *, dry_run: {"pr": n, "action": a.action, "status": "dry-run"})
    monkeypatch.setattr("pipeline.bot_token.get", lambda: None)
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
    monkeypatch.setattr("pipeline.bot_token.get", lambda: None)
    evs = _drain(bulk.run_cluster([models.ClusterItem(pr=1, action="needs-human")], dry_run=True))
    [res] = [json.loads(e["data"]) for e in evs if e["event"] == "result"]
    assert res["status"] == "skipped"



def test_run_cluster_skips_closes_when_a_merge_fails(monkeypatch):
    # #188: a failed merge must skip the dependent closes (don't orphan-close).
    closed = []
    monkeypatch.setattr(executor, "merge_pr",
                        lambda n, method, *, dry_run, reason=None: {"pr": n, "action": "MERGE", "status": "error", "detail": "boom"})
    monkeypatch.setattr(executor, "execute_pr",
                        lambda n, a, *, dry_run: closed.append(n) or {"pr": n, "action": a.action, "status": "dry-run"})
    monkeypatch.setattr("pipeline.bot_token.get", lambda: None)
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
                        lambda n, a, *, dry_run: closed.append(n) or {"pr": n, "action": a.action, "status": "closed"})
    monkeypatch.setattr("pipeline.bot_token.get", lambda: "tok")
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
    from prospector_app.backend import app as appmod
    monkeypatch.setattr(appmod.executor, "execute_pr",
                        lambda n, a, *, dry_run: {"pr": n, "action": a.action,
                                                         "status": "dry-run", "detail": "ok"})
    monkeypatch.setattr("pipeline.bot_token.get", lambda: None)
    monkeypatch.setattr(appmod.bulk.training, "capture", lambda *a, **k: None)
    c = TestClient(appmod.app)
    r = c.post("/api/execute/cluster",
               json={"items": [{"pr": 1, "action": "close-dup", "canonical": 9}], "dry_run": True})
    assert r.status_code == 200
    assert "result" in r.text and "done" in r.text


def _loop_ticks_while(gen) -> int:
    """Drain `gen` while a coroutine counts 10 ms sleeps on the same loop."""
    async def go() -> int:
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1
        ticker = asyncio.create_task(tick())
        [ev async for ev in gen]
        ticker.cancel()
        return ticks
    return asyncio.run(go())


def test_bulk_executor_calls_leave_the_event_loop_free(monkeypatch):
    def slow_exec(n, action, *, dry_run):
        time.sleep(0.2)
        return {"pr": n, "action": action.action, "status": "dry-run"}
    monkeypatch.setattr(executor, "execute_pr", slow_exec)
    monkeypatch.setattr("pipeline.bot_token.get", lambda: None)

    assert _loop_ticks_while(bulk.run_bulk([1, 2], "CLOSE", dry_run=True)) >= 10


def test_cluster_executor_calls_leave_the_event_loop_free(monkeypatch):
    def slow_merge(n, method, *, dry_run, reason=None):
        time.sleep(0.2)
        return {"pr": n, "action": "MERGE", "status": "merged"}
    monkeypatch.setattr(executor, "merge_pr", slow_merge)
    monkeypatch.setattr("pipeline.bot_token.get", lambda: "tok")
    monkeypatch.setattr(bulk.training, "capture", lambda *a, **k: None)
    items = [models.ClusterItem(pr=1, action="merge"), models.ClusterItem(pr=2, action="merge")]

    assert _loop_ticks_while(bulk.run_cluster(items, dry_run=False)) >= 10
