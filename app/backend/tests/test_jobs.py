import asyncio
import json
import sys

import pytest
from app.backend import jobs


def test_triage_spec_is_listed_and_needs_cluster():
    specs = {s["kind"]: s for s in jobs.list_specs()}
    assert "triage-cluster" in specs
    assert specs["triage-cluster"]["needs_cluster"] is True
    assert "reformat-rationales" not in specs


def test_triage_argv_includes_cluster_and_script():
    jobs.JOBS.clear()
    job = jobs.start_job("triage-cluster", 203)
    argv = job["_argv"]
    assert argv[-2:] == ["--cluster", "203"]
    assert any(a.endswith("triage_cluster.py") for a in argv)


def test_triage_without_cluster_rejected():
    with pytest.raises(ValueError):
        jobs.start_job("triage-cluster", None)


def test_second_concurrent_triage_same_cluster_rejected():
    jobs.JOBS.clear()
    jobs.start_job("triage-cluster", 203)  # left "running"
    with pytest.raises(ValueError):
        jobs.start_job("triage-cluster", 203)
    # a different cluster is fine
    jobs.start_job("triage-cluster", 204)


def test_security_review_spec_is_listed_and_needs_pr():
    specs = {s["kind"]: s for s in jobs.list_specs()}
    assert "security-review" in specs
    assert specs["security-review"]["needs_pr"] is True
    assert specs["security-review"]["needs_cluster"] is False


def test_security_review_argv_includes_pr_and_script():
    jobs.JOBS.clear()
    job = jobs.start_job("security-review", pr=4242)
    argv = job["_argv"]
    assert argv[-2:] == ["--pr", "4242"]
    assert any(a.endswith("security_review.py") for a in argv)
    assert job["pr"] == 4242


def test_security_review_without_pr_rejected():
    with pytest.raises(ValueError):
        jobs.start_job("security-review", None)


def test_second_concurrent_security_review_same_pr_rejected():
    jobs.JOBS.clear()
    jobs.start_job("security-review", pr=4242)  # left "running"
    with pytest.raises(ValueError):
        jobs.start_job("security-review", pr=4242)
    # a different PR is fine
    jobs.start_job("security-review", pr=4343)


def test_threat_scan_pr_spec_is_listed_and_needs_pr():
    specs = {s["kind"]: s for s in jobs.list_specs()}
    assert "threat-scan-pr" in specs
    assert specs["threat-scan-pr"]["needs_pr"] is True
    assert specs["threat-scan-pr"]["needs_cluster"] is False


def test_threat_scan_pr_argv_includes_only_flag_and_script():
    jobs.JOBS.clear()
    job = jobs.start_job("threat-scan-pr", pr=4242)
    argv = job["_argv"]
    assert argv[-2:] == ["--only", "4242"]
    assert any(a.endswith("threat_scan.py") for a in argv)
    assert job["pr"] == 4242


def test_threat_scan_pr_without_pr_rejected():
    with pytest.raises(ValueError):
        jobs.start_job("threat-scan-pr", None)


def test_second_concurrent_threat_scan_pr_same_pr_rejected():
    jobs.JOBS.clear()
    jobs.start_job("threat-scan-pr", pr=4242)  # left "running"
    with pytest.raises(ValueError):
        jobs.start_job("threat-scan-pr", pr=4242)
    # a different PR is fine
    jobs.start_job("threat-scan-pr", pr=4343)


def test_issue_find_fixed_job_registered():
    spec = jobs.JOB_SPECS["issue-find-fixed"]
    assert spec["needs_count"] is True
    argv = spec["argv_fn"](12)
    assert any("find_fixed.py" in str(a) for a in argv)
    assert "12" in [str(a) for a in argv]


def test_analyze_clusters_spec_is_listed_and_needs_count():
    specs = {s["kind"]: s for s in jobs.list_specs()}
    assert "analyze-clusters" in specs
    assert specs["analyze-clusters"]["needs_count"] is True
    assert specs["analyze-clusters"]["needs_cluster"] is False


def test_analyze_clusters_argv_includes_limit_and_script():
    jobs.JOBS.clear()
    job = jobs.start_job("analyze-clusters", count=20)
    argv = job["_argv"]
    assert argv[-2:] == ["--limit", "20"]
    assert any(a.endswith("analyze_clusters.py") for a in argv)


def test_analyze_clusters_without_count_rejected():
    with pytest.raises(ValueError):
        jobs.start_job("analyze-clusters", count=None)


# --- #683: a job runs to completion independent of any SSE reader, and a ---
# --- reattached reader always sees the full history, not just what's new. ---

def _bare_job(**over) -> dict:
    job = {"log": [], "status": "running", "returncode": None, "_wake": asyncio.Event()}
    job.update(over)
    return job


async def _collect(gen) -> list[dict]:
    return [ev async for ev in gen]


def test_run_job_completes_with_zero_listeners(monkeypatch):
    """The core regression: previously spawning+draining only happened inside
    the generator an SSE client was actively consuming, so a job with no
    attached reader never advanced past "running". `run_job` must finish a
    real subprocess and land its status/returncode on its own."""
    monkeypatch.setattr(jobs.data, "refresh", lambda: None)
    job = _bare_job(_argv=[sys.executable, "-u", "-c", "print('hello')"])
    asyncio.run(jobs.run_job(job))
    assert job["status"] == "done"
    assert job["returncode"] == 0
    assert "hello" in job["log"]


def test_run_job_records_failure_without_a_listener(monkeypatch):
    monkeypatch.setattr(jobs.data, "refresh", lambda: None)
    job = _bare_job(_argv=[sys.executable, "-u", "-c", "import sys; sys.exit(3)"])
    asyncio.run(jobs.run_job(job))
    assert job["status"] == "failed"
    assert job["returncode"] == 3


def test_run_job_survives_a_spawn_error(monkeypatch):
    """A child that can't even start (bad argv) fails the job instead of
    raising out of the background task — an unawaited exception there would
    otherwise be silently dropped or crash the event loop."""
    monkeypatch.setattr(jobs.data, "refresh", lambda: None)
    job = _bare_job(_argv=["/no/such/executable-698234"])
    asyncio.run(jobs.run_job(job))
    assert job["status"] == "failed"
    assert job["returncode"] == -1


def test_attach_job_replays_full_log_to_a_late_attacher():
    job = _bare_job(log=["line1", "line2"], status="done", returncode=0)
    events = asyncio.run(_collect(jobs.attach_job(job)))
    assert [e["data"] for e in events if e["event"] == "log"] == ["line1", "line2"]
    done = json.loads(next(e["data"] for e in events if e["event"] == "done"))
    assert done == {"returncode": 0, "status": "done"}


def test_attach_job_follows_live_output_until_done():
    async def run():
        job = _bare_job()
        events = []

        async def consume():
            async for e in jobs.attach_job(job):
                events.append(e)

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0)  # let the consumer reach its first wait
        jobs._emit(job, "line1")
        await asyncio.sleep(0)
        jobs._emit(job, "line2")
        await asyncio.sleep(0)
        job["status"] = "done"
        job["returncode"] = 0
        job["_wake"].set()
        await consumer
        return events

    events = asyncio.run(run())
    assert [e["data"] for e in events if e["event"] == "log"] == ["line1", "line2"]


def test_reattach_after_abandoning_stream_sees_full_history():
    """Simulates navigating away mid-run (the first reader is abandoned via
    `aclose()`, as an SSE disconnect would do) then reattaching later — the
    reattached stream must show everything, not just lines emitted after it
    connected."""
    async def run():
        job = _bare_job()
        jobs._emit(job, "line1")

        gen1 = jobs.attach_job(job)
        first = await gen1.__anext__()
        assert first == {"event": "log", "data": "line1"}
        await gen1.aclose()  # the abandoned reader — must not affect the job

        jobs._emit(job, "line2")
        job["status"] = "done"
        job["returncode"] = 0
        job["_wake"].set()

        return await _collect(jobs.attach_job(job))

    events = asyncio.run(run())
    assert [e["data"] for e in events if e["event"] == "log"] == ["line1", "line2"]
