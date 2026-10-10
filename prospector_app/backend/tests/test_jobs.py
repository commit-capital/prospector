import asyncio
import json
import subprocess
import sys
import time

import pytest
from prospector_app.backend import jobs


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
    jobs.start_job("triage-cluster", 203)  # left active
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
    assert job["status"] == "queued"


def test_security_review_without_pr_rejected():
    with pytest.raises(ValueError):
        jobs.start_job("security-review", None)


def test_second_concurrent_security_review_same_pr_rejected():
    jobs.JOBS.clear()
    jobs.start_job("security-review", pr=4242)  # left active
    with pytest.raises(ValueError):
        jobs.start_job("security-review", pr=4242)
    # a different PR is fine
    jobs.start_job("security-review", pr=4343)


def test_security_job_scheduler_bounds_concurrency(monkeypatch):
    active = 0
    peak = 0

    async def fake_run_job(job):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1

    monkeypatch.setattr(jobs, "run_job", fake_run_job)

    async def run_batch():
        await asyncio.gather(*(jobs._run_scheduled_job(
            {"id": n, "kind": "security-review"}) for n in range(5)))

    asyncio.run(run_batch())
    assert peak == 2


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
    jobs.start_job("threat-scan-pr", pr=4242)  # left active
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


def _job(argv: list[str]) -> dict:
    job = jobs.start_job("selftest")
    job["_argv"] = argv
    return job


async def _collect(gen) -> list[dict]:
    return [ev async for ev in gen]


def _logged(events: list[dict]) -> list[str]:
    return [e["data"] for e in events if e["event"] == "log"]


@pytest.fixture
def no_refresh(monkeypatch):
    monkeypatch.setattr(jobs, "_refresh_snapshots", lambda: None)


def test_run_job_completes_with_zero_listeners(no_refresh):
    job = _job([sys.executable, "-u", "-c", "print('hello')"])
    asyncio.run(jobs.run_job(job))
    assert job["status"] == "done"
    assert job["returncode"] == 0
    assert "hello" in job["log"]
    assert job["finished"] is not None


def test_run_job_records_failure_without_a_listener(no_refresh):
    job = _job([sys.executable, "-u", "-c", "import sys; sys.exit(3)"])
    asyncio.run(jobs.run_job(job))
    assert job["status"] == "failed"
    assert job["returncode"] == 3


def test_run_job_fails_a_command_that_cannot_start(no_refresh):
    job = _job(["/no/such/executable-698234"])
    asyncio.run(jobs.run_job(job))
    assert job["status"] == "failed"
    assert job["returncode"] not in (0, None)


def test_run_job_keeps_output_and_ending_on_disk(no_refresh):
    job = _job([sys.executable, "-u", "-c", "print('hello')"])
    asyncio.run(jobs.run_job(job))
    record = json.loads((jobs.JOBS_DIR / f"{job['id']}.json").read_text())
    assert record["status"] == "done" and record["returncode"] == 0
    assert "hello" in (jobs.JOBS_DIR / f"{job['id']}.log").read_text()


def test_a_finished_job_leaves_the_issue_and_alert_snapshots_current(monkeypatch, tmp_path):
    from prospector_app.backend import alert_data, issue_data
    monkeypatch.setattr(jobs.data, "refresh", lambda: None)
    issue_data.set_store_root(tmp_path / "issues")
    alert_data.set_store_root(tmp_path / "alerts")
    try:
        assert not issue_data.issues() and not issue_data.runs() and not alert_data.runs()
        issue_data.store().create_issue(1, {"title": "Bug", "state": "open",
                                            "updated_at": "2026-06-23T00:00:00Z"})
        for module in (issue_data, alert_data):
            module.store().append_run({"phase": "ingest", "finished": "2026-06-24T00:00:00+00:00"})
        asyncio.run(jobs.run_job(_job([sys.executable, "-c", "pass"])))
        assert set(issue_data.issues()) == {1}
        assert len(issue_data.runs()) == len(alert_data.runs()) == 1
    finally:
        issue_data.set_store_root(None)
        alert_data.set_store_root(None)


def test_job_ids_keep_rising_across_a_restart():
    first = jobs.start_job("selftest")
    jobs.JOBS.clear()
    assert jobs.start_job("selftest")["id"] == first["id"] + 1


def test_a_job_outlives_its_backend_and_is_followed_after_restart(no_refresh):
    """The dev server's reload kills the task following a job; the child keeps
    running, and the restarted backend picks it up from its log and exit files."""
    script = ("import time; print('one', flush=True); time.sleep(0.8); "
              "print('two', flush=True)")

    async def first_backend() -> int:
        job = _job([sys.executable, "-u", "-c", script])
        task = asyncio.create_task(jobs.run_job(job))
        while "one" not in job["log"]:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert job["status"] == "running"
        return job["id"]

    job_id = asyncio.run(first_backend())
    jobs.JOBS.clear()

    async def second_backend():
        jobs.restore()
        job = jobs.JOBS[job_id]
        assert job["status"] == "running"
        return job, await _collect(jobs.attach_job(job))

    job, events = asyncio.run(second_backend())
    assert job["status"] == "done" and job["returncode"] == 0
    assert {"one", "two"} <= set(_logged(events))


def _record_running(job: dict, pid: int) -> None:
    job["status"] = "running"
    job["pid"] = pid
    jobs._save(job)
    jobs.JOBS.clear()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_restore_fails_a_job_whose_process_vanished(no_refresh):
    job = jobs.start_job("selftest")
    _record_running(job, _dead_pid())
    asyncio.run(_restore())
    restored = jobs.JOBS[job["id"]]
    assert restored["status"] == "failed" and restored["returncode"] is None
    assert any("lost" in line for line in restored["log"])


def test_restore_settles_a_job_whose_exit_was_recorded(no_refresh):
    job = jobs.start_job("selftest")
    (jobs.JOBS_DIR / f"{job['id']}.exit").write_text("0\n")
    _record_running(job, _dead_pid())
    asyncio.run(_restore())
    assert jobs.JOBS[job["id"]]["status"] == "done"


def test_restore_schedules_a_job_that_never_started(monkeypatch):
    scheduled = []
    monkeypatch.setattr(jobs, "schedule_job", lambda job: scheduled.append(job["id"]))
    job = jobs.start_job("selftest")
    jobs.JOBS.clear()
    asyncio.run(_restore())
    assert scheduled == [job["id"]]


async def _restore() -> None:
    jobs.restore()


def test_attach_job_announces_the_job_then_resumes_after_a_line_count():
    job = jobs.start_job("selftest")
    job.update(log=["line1", "line2", "line3"], status="done", returncode=0)
    events = asyncio.run(_collect(jobs.attach_job(job, after=2)))
    assert events[0]["event"] == "job"
    assert json.loads(events[0]["data"])["id"] == job["id"]
    assert _logged(events) == ["line3"]


def test_stop_job_ends_a_running_job(no_refresh):
    async def run():
        job = _job([sys.executable, "-u", "-c",
                    "import time; print('up', flush=True); time.sleep(30)"])
        task = asyncio.create_task(jobs.run_job(job))
        while "up" not in job["log"]:
            await asyncio.sleep(0.01)
        await jobs.stop_job(job["id"])
        await asyncio.wait_for(task, 10)
        return job

    job = asyncio.run(run())
    assert job["status"] == "failed"
    assert any("stopped by the operator" in line for line in job["log"])


def test_stop_job_reaches_a_child_in_a_session_of_its_own(no_refresh, tmp_path):
    """A job's agents and sandbox launchers start their own sessions, out of
    reach of the job's own process group."""
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " start_new_session=True)\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "print('up', flush=True)\n"
        "time.sleep(60)\n")

    async def run() -> int:
        job = _job([sys.executable, "-u", "-c", script])
        task = asyncio.create_task(jobs.run_job(job))
        while "up" not in job["log"]:
            await asyncio.sleep(0.01)
        await jobs.stop_job(job["id"])
        await asyncio.wait_for(task, 10)
        return int(pid_file.read_text())

    child = asyncio.run(run())
    for _ in range(200):
        if not jobs._alive(child):
            break
        time.sleep(0.02)
    assert not jobs._alive(child)


def test_stop_job_cancels_a_queued_job_before_it_starts(no_refresh):
    job = jobs.start_job("selftest")
    asyncio.run(jobs.stop_job(job["id"]))
    asyncio.run(jobs.run_job(job))
    assert job["status"] == "failed" and job["pid"] is None


def test_a_line_longer_than_one_read_is_taken_whole():
    job = jobs.start_job("selftest")
    (jobs.JOBS_DIR / f"{job['id']}.log").write_bytes(b"x" * (jobs.CATCH_UP_BYTES + 10) + b"\nnext\n")
    jobs._catch_up(job)
    jobs._catch_up(job)
    jobs._catch_up(job)
    assert [len(line) for line in job["log"]] == [jobs.CATCH_UP_BYTES, 10, 4]


def test_finished_jobs_beyond_the_keep_count_are_pruned(monkeypatch):
    monkeypatch.setattr(jobs, "KEEP_FINISHED", 2)
    old = [jobs.start_job("selftest") for _ in range(3)]
    for job in old:
        job["status"] = "done"
    newest = jobs.start_job("selftest")
    assert sorted(jobs.JOBS) == [old[1]["id"], old[2]["id"], newest["id"]]
    assert not (jobs.JOBS_DIR / f"{old[0]['id']}.json").exists()


def test_runner_lines_land_on_their_own_line_after_partial_output():
    job = jobs.start_job("selftest")
    (jobs.JOBS_DIR / f"{job['id']}.log").write_bytes(b"half a line")
    jobs._append(job, "! note")
    jobs._catch_up(job, final=True)
    assert job["log"] == ["half a line", "! note"]


def test_carriage_returns_keep_the_last_rewrite_of_a_line():
    job = jobs.start_job("selftest")
    (jobs.JOBS_DIR / f"{job['id']}.log").write_bytes(b"10%\r50%\r100%\r\n")
    jobs._catch_up(job)
    assert job["log"] == ["100%"]


def test_attach_job_replays_full_log_to_a_late_attacher():
    job = jobs.start_job("selftest")
    job.update(log=["line1", "line2"], status="done", returncode=0)
    events = asyncio.run(_collect(jobs.attach_job(job)))
    assert _logged(events) == ["line1", "line2"]
    done = json.loads(next(e["data"] for e in events if e["event"] == "done"))
    assert done == {"returncode": 0, "status": "done", "finished": None}


def test_attach_job_follows_live_output_until_done():
    async def run():
        job = jobs.start_job("selftest")
        job["status"] = "running"
        events = []

        async def consume():
            async for e in jobs.attach_job(job):
                events.append(e)

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0)  # let the consumer reach its first wait
        jobs._append(job, "line1")
        jobs._catch_up(job)
        await asyncio.sleep(0)
        jobs._append(job, "line2")
        jobs._catch_up(job)
        await asyncio.sleep(0)
        job["status"] = "done"
        job["returncode"] = 0
        jobs._notify(job)
        await consumer
        return events

    events = asyncio.run(run())
    assert _logged(events) == ["line1", "line2"]


def test_reattach_after_abandoning_stream_sees_full_history():
    """Simulates navigating away mid-run (the first reader is abandoned via
    `aclose()`, as an SSE disconnect would do) then reattaching later — the
    reattached stream must show everything, not just lines emitted after it
    connected."""
    async def run():
        job = jobs.start_job("selftest")
        job["status"] = "running"
        jobs._append(job, "line1")

        gen1 = jobs.attach_job(job)
        assert (await gen1.__anext__())["event"] == "job"
        assert await gen1.__anext__() == {"event": "log", "data": "line1"}
        await gen1.aclose()  # the abandoned reader — must not affect the job

        jobs._append(job, "line2")
        job["status"] = "done"
        job["returncode"] = 0
        jobs._notify(job)

        return await _collect(jobs.attach_job(job))

    events = asyncio.run(run())
    assert _logged(events) == ["line1", "line2"]


def test_attach_job_group_replays_and_follows_statuses():
    async def run():
        queued = _bare_job(id=1, status="queued")
        finished = _bare_job(id=2, status="done", returncode=0)
        events = []

        async def consume():
            async for event in jobs.attach_job_group([queued, finished]):
                events.append(event)

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0)
        queued["status"] = "running"
        jobs._notify(queued)
        while len(events) < 3:
            await asyncio.sleep(0)
        queued["status"] = "done"
        queued["returncode"] = 0
        queued["_wake"].set()
        await consumer
        return events

    events = asyncio.run(run())
    updates = [json.loads(event["data"]) for event in events if event["event"] == "job"]
    assert updates == [
        {"id": 1, "status": "queued", "returncode": None},
        {"id": 2, "status": "done", "returncode": 0},
        {"id": 1, "status": "running", "returncode": None},
        {"id": 1, "status": "done", "returncode": 0},
    ]
    assert json.loads(events[-1]["data"]) == {"jobs": 2}


def test_job_group_route_streams_all_requested_jobs_once():
    from fastapi.testclient import TestClient
    from prospector_app.backend import app as appmod

    jobs.JOBS.clear()
    first = jobs.start_job("selftest")
    second = jobs.start_job("selftest")
    for job in (first, second):
        job["status"] = "done"
        job["returncode"] = 0

    response = TestClient(appmod.app).get(
        "/api/jobs/stream/group",
        params=[("job_id", first["id"]), ("job_id", second["id"]),
                ("job_id", first["id"])],
    )

    assert response.status_code == 200
    assert response.text.count("event: job") == 2
    assert "event: done" in response.text


def test_security_sweep_replaces_the_alert_jobs():
    assert "alert-ingest" not in jobs.JOB_SPECS and "alert-find-fixed" not in jobs.JOB_SPECS
    spec = jobs.JOB_SPECS["security-sweep"]
    assert spec.get("needs_count") is True
    argv = spec["argv_fn"](7)
    assert argv[-3:] == [str(jobs.REPO_ROOT / "alert_triage" / "security_sweep.py"),
                         "--limit", "7"]


def _client():
    from fastapi.testclient import TestClient
    from prospector_app.backend import app as appmod
    return TestClient(appmod.app)


def test_job_routes_read_resume_and_refuse_to_stop_a_finished_job():
    job = jobs.start_job("selftest")
    job.update(log=["a", "b"], status="done", returncode=0)
    client = _client()

    assert client.get("/api/jobs/specs").status_code == 200
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "done"
    assert client.get("/api/jobs/999").status_code == 404
    stream = client.get(f"/api/jobs/{job['id']}/stream", params={"after": 1}).text
    assert "data: b" in stream and "data: a\r\n" not in stream
    assert client.post(f"/api/jobs/{job['id']}/stop").status_code == 409


def test_job_specs_answer_without_reading_the_runs_ledgers(monkeypatch):
    from prospector_app.backend import pipeline_status

    def ledger_read():
        raise AssertionError("the specs route read the runs ledgers")
    monkeypatch.setattr(pipeline_status, "job_runtimes", ledger_read)

    specs = _client().get("/api/jobs/specs").json()["specs"]

    assert {s["kind"] for s in specs} == set(jobs.JOB_SPECS)


def test_job_runtimes_route_serves_the_ledger_runtimes(monkeypatch):
    from prospector_app.backend import pipeline_status
    runtime = {"last_run": "2026-07-05T10:03:00+00:00", "typical_seconds": 180.0,
               "typical_count": None}
    monkeypatch.setattr(pipeline_status, "job_runtimes", lambda: {"ingest": runtime})

    assert _client().get("/api/jobs/runtimes").json() == {"runtimes": {"ingest": runtime}}


def test_count_jobs_name_their_own_default_and_noun():
    specs = {s["kind"]: s for s in jobs.list_specs()}
    assert (specs["security-sweep"]["count_default"], specs["security-sweep"]["count_noun"]) == (12, "records")
    assert specs["issue-find-fixed"]["count_default"] == 200
    assert specs["issue-analyze"]["count_default"] == 200
    assert specs["analyze-clusters"]["count_default"] == 20
    assert all(s["count_default"] is not None for s in specs.values() if s["needs_count"])
