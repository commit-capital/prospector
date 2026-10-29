"""What each machine did in the past day: the runs ledgers, agent cost, the
machine roster and this app's jobs folded per machine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from prospector_app.backend import app as appmod
from pipeline import store as S
from prospector_app.backend import data
from prospector_app.backend import machine_activity as ma

NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
JOB_PHASES = {("pr", "ingest"): ("ingest", "Ingest"),
              ("issue", "ingest"): ("issue-ingest", "Issue ingest"),
              ("pr", "cluster:summaries"): ("cluster-new", "Cluster new PRs"),
              ("issue", "feature-screen:run"): ("feature-screen", "Feature-request screen")}


def _at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def _row(phase: str, host: str | None = "studio", ledger: str = "pr", hours_ago: float = 1,
         **extra) -> tuple[str, dict]:
    raw: dict = {"phase": phase, "started": _at(hours_ago + 0.1), "finished": _at(hours_ago), **extra}
    if host is not None:
        raw["host"] = host
    return ledger, raw


def _roster(*machines: dict, local: str = "laptop") -> dict:
    return {"machines": list(machines), "local": local}


def _machine(host: str, online: bool = True, last_beat: str | None = None,
             tripped: tuple[str, ...] = (), current_pr: int | None = None) -> dict:
    beat = {"last_beat": last_beat or _at(0), "online": online,
            "current_pr": current_pr, "current_issue": None, "autohunt": True}
    return {"host": host, "online": online, "beats": {"verify": beat},
            "lanes": {lane: {"tripped": True} for lane in tripped},
            "base_pinned": True, "ai_account": None}


def _summarize(rows: list[tuple[str, dict]], roster: dict | None = None,
               cost: dict[str, float] | None = None, local_jobs: list[dict] | None = None) -> dict:
    return ma.summarize(rows, cost or {}, roster or _roster(), local_jobs or [],
                        JOB_PHASES, NOW)


def _host(view: dict, host: str) -> dict:
    return next(m for m in view["machines"] if m["host"] == host)


def test_lane_outcomes():
    rows = [
        _row("security:review-one", pr=1, stats={"verdict": "GREEN"}),
        _row("security:review-one", pr=2, stats={"verdict": "YELLOW"}),
        _row("security:review-one", pr=3, stats={"verdict": "GREEN"}),
        _row("verify:single", pr=4, stats={"status": "done", "outcome": "verified-fix"}),
        _row("verify:single", pr=5, stats={"status": "waiting-for-base", "error_kind": "no-base"}),
        _row("verify:single", pr=6, stats={"status": "error", "error_kind": "sandbox-error"}),
        _row("fix:single", host=None, pr=7, stats={"host": "studio", "status": "awaiting-review"}),
        _row("fix:single", host=None, pr=7, stats={"host": "studio", "status": "failed"}),
        _row("issue-fix:run", host=None, ledger="issue", issue=70,
             stats={"host": "studio", "ending": "fixed"}),
    ]
    lanes = _host(_summarize(rows), "studio")["lanes"]
    assert lanes["security"] == {"count": 3, "numbers": [1, 2, 3], "outcomes": [
        {"label": "GREEN", "count": 2, "numbers": [1, 3]},
        {"label": "YELLOW", "count": 1, "numbers": [2]}]}
    assert [o["label"] for o in lanes["verify"]["outcomes"]] == [
        "verified-fix", "waiting for base", "error: sandbox-error"]
    assert lanes["autofix"]["count"] == 2 and lanes["autofix"]["numbers"] == [7]
    assert sorted(o["label"] for o in lanes["autofix"]["outcomes"]) == ["failed", "parked"]
    assert lanes["issue_fix"] == {"count": 1, "numbers": [70], "outcomes": [
        {"label": "fixed", "count": 1, "numbers": [70]}]}


def test_an_autofix_held_for_ai_capacity_reads_as_waiting_not_failed():
    rows = [
        _row("fix:single", host=None, pr=7, stats={"host": "studio", "status": "failed",
                                                   "kind": "capacity-paused"}),
        _row("fix:single", host=None, pr=8, stats={"host": "studio", "status": "failed",
                                                   "kind": "sandbox"}),
    ]
    outcomes = _host(_summarize(rows), "studio")["lanes"]["autofix"]["outcomes"]
    assert sorted((o["label"], o["numbers"]) for o in outcomes) == [
        ("failed", [8]), ("waiting for AI capacity", [7])]


def test_window_edge():
    rows = [_row("ingest:watch", hours_ago=24 + 1 / 60), _row("ingest:watch", hours_ago=24 - 1 / 60)]
    assert _host(_summarize(rows), "studio")["background"] == [{"label": "PR watch", "count": 1}]


def test_row_without_timestamps_is_skipped():
    view = _summarize([("pr", {"phase": "ingest:watch", "host": "studio"})])
    assert all(m["host"] != "studio" for m in view["machines"])


def test_host_from_stats_then_unattributed():
    view = _summarize([_row("ingest:watch", host=None), _row("ingest:watch", host="studio")],
                      roster=_roster(_machine("studio")))
    assert [m["host"] for m in view["machines"]] == ["studio", "unattributed"]
    assert _host(view, "unattributed")["background"] == [{"label": "PR watch", "count": 1}]


def test_background_labels_and_worker_cluster_rows():
    rows = [_row("ingest:watch", trigger="worker"), _row("ingest:watch", trigger="worker"),
            _row("threat-scan:heads", trigger="worker"),
            _row("cluster:summaries", trigger="worker"),
            _row("cluster:summaries", trigger="cli")]
    m = _host(_summarize(rows), "studio")
    assert m["background"] == [{"label": "PR watches", "count": 2},
                               {"label": "threat scan", "count": 1},
                               {"label": "summary batch", "count": 1}]
    assert m["jobs"] == [{"label": "Cluster new PRs", "kind": "cluster-new",
                          "counts": {"done": 1}, "job_id": None, "running_id": None}]


def test_feature_screens_read_as_worker_background_or_as_their_control_job():
    rows = [_row("feature-screen:run", ledger="issue", trigger="worker"),
            _row("feature-screen:run", ledger="issue", trigger="worker"),
            _row("feature-screen:run", ledger="issue", trigger="backfill")]
    m = _host(_summarize(rows), "studio")
    assert m["background"] == [{"label": "feature screens", "count": 2}]
    assert [j["kind"] for j in m["jobs"]] == ["feature-screen"]


def test_the_feature_screen_job_reads_the_phase_its_runs_book():
    assert ma._job_phases()[("issue", "feature-screen:run")][0] == "feature-screen"


def test_job_phase_is_keyed_by_ledger():
    rows = [_row("ingest"), _row("ingest", ledger="issue"), _row("ingest")]
    assert [j["label"] for j in _host(_summarize(rows), "studio")["jobs"]] == ["Ingest", "Issue ingest"]


def test_local_jobs_replace_ledger_jobs():
    jobs = [{"id": 9, "kind": "threat-scan", "label": "Threat scan", "status": "failed",
             "started": _at(2), "finished": _at(1.9), "cluster": None, "pr": None,
             "count": None, "returncode": 1},
            {"id": 3, "kind": "ingest", "label": "Ingest", "status": "done",
             "started": _at(30), "finished": _at(29.9), "cluster": None, "pr": None,
             "count": None, "returncode": 0}]
    m = _host(_summarize([_row("ingest", host="laptop")], local_jobs=jobs), "laptop")
    assert m["local"] is True
    assert m["jobs"] == [{"label": "Threat scan", "kind": "threat-scan", "counts": {"failed": 1},
                          "job_id": 9, "running_id": None}]


def _local_job(job_id: int, status: str, kind: str = "design-review-pr",
               label: str = "Design review (single PR)") -> dict:
    return {"id": job_id, "kind": kind, "label": label, "status": status, "started": _at(1),
            "finished": None, "cluster": None, "pr": 100 + job_id, "count": None, "returncode": None}


def test_local_jobs_of_one_kind_fold_into_counts():
    local = [_local_job(n, "done") for n in range(1, 4)] + [
        _local_job(4, "failed"), _local_job(5, "running"), _local_job(6, "queued"),
        _local_job(7, "queued"), _local_job(8, "done", kind="ingest", label="Ingest")]
    m = _host(_summarize([], local_jobs=local), "laptop")
    assert m["jobs"] == [
        {"label": "Design review (single PR)", "kind": "design-review-pr",
         "counts": {"done": 3, "failed": 1, "running": 1, "queued": 2},
         "job_id": 7, "running_id": 5},
        {"label": "Ingest", "kind": "ingest", "counts": {"done": 1}, "job_id": 8, "running_id": None}]


def test_offline_roster_machine_without_rows():
    beat = _at(50)
    m = _host(_summarize([], roster=_roster(_machine("old", online=False, last_beat=beat,
                                                     tripped=("security",)))), "old")
    assert m["online"] is False and m["offline_since"] == beat and m["has_worker"] is True
    assert m["tripped"] == ["security"]
    assert m["lanes"] == {} and m["background"] == [] and m["jobs"] == []


def test_current_work_comes_from_an_online_beat():
    m = _host(_summarize([], roster=_roster(_machine("studio", current_pr=9566))), "studio")
    assert m["current"] == {"pr": 9566, "issue": None}


def test_online_machines_first():
    view = _summarize([], roster=_roster(_machine("b-off", online=False), _machine("z-on")))
    assert [m["host"] for m in view["machines"]] == ["z-on", "b-off"]


def test_cost_is_shown_on_the_roster_machines_only():
    view = _summarize([], roster=_roster(_machine("studio")),
                      cost={"studio": 3.75, "gone": 9.0})
    assert _host(view, "studio")["cost_usd"] == 3.75
    assert [m["host"] for m in view["machines"]] == ["studio"]


def test_activity_sums_the_past_days_agent_cost_per_host(tmp_path, monkeypatch):
    st = S.Store(tmp_path)
    for host, cost, hours_ago in (("studio", 1.25, 1), ("studio", 2.5, 2), ("studio", 9.0, 30),
                                  ("laptop", None, 1)):
        ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        st.append_agent_run({"phase": "agent:run", "host": host, "cost_usd": cost,
                             "started": ts, "finished": ts, "ts": ts})
    monkeypatch.setattr(data, "store", lambda: st)
    seen = {}
    monkeypatch.setattr(ma, "summarize", lambda rows, cost, *a: seen.setdefault("cost", cost))
    ma.activity()
    assert seen["cost"] == {"studio": 3.75}


def test_route_answers(monkeypatch):
    monkeypatch.setattr(ma, "activity", lambda: {"local": "x", "window_hours": 24, "machines": []})
    r = TestClient(appmod.app, raise_server_exceptions=False).get("/api/machines/activity")
    assert r.status_code == 200 and r.json()["window_hours"] == 24


def test_one_background_pass_reads_singular():
    rows = [_row("cluster:summaries", trigger="worker"), _row("ingest:watch"),
            _row("analyze:commit", trigger="worker"), _row("analyze:commit", trigger="worker")]
    assert _host(_summarize(rows), "studio")["background"] == [
        {"label": "cluster analyses", "count": 2},
        {"label": "summary batch", "count": 1},
        {"label": "PR watch", "count": 1}]


def test_cost_rounds_once_after_summing():
    view = _summarize([], roster=_roster(_machine("studio")), cost={"studio": 0.013 * 300})
    assert _host(view, "studio")["cost_usd"] == 3.9


def test_a_worker_silent_past_the_offline_window_is_stalled():
    view = _summarize([], roster=_roster(_machine("brief", online=False, last_beat=_at(0.05)),
                                         _machine("long", online=False, last_beat=_at(2))))
    assert _host(view, "brief")["stalled"] is False
    assert _host(view, "long")["stalled"] is True
