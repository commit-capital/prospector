"""The Health & queues overview: machines, lanes, loops, needs, timeline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prospector_app.backend import control_overview as co

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
ALL_ON = {lane: True for lane in co.LANES}


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _machine(host: str, beat_min: float, **beats: dict) -> dict:
    return {"host": host, "online": beat_min < 2, "ai_account": {"key": "acct", "label": "x"},
            "beats": beats or {"verify": {"last_beat": _iso(beat_min), "online": beat_min < 2,
                                          "current_pr": None, "current_issue": None}}}


def _inputs(**over) -> co.Inputs:
    base: co.Inputs = {
        "local": "studio",
        "roster": {"local": "studio", "machines": [_machine("studio", 0.5)]},
        "stood_down": {}, "loops": {"studio": {"lanes": ALL_ON, "loops": {}}},
        "tripped": {}, "pins": [], "ai_paused": {},
        "verify_queue": [], "fix_queue": [], "issue_queue": [], "hunter_now": {},
        "pools": {"security": 0, "verify": 0, "design": 0},
        "failed_runs": [], "rows": [], "paused": {}, "picks": {}}
    base.update(over)  # type: ignore[typeddict-item]
    return base


def _cell(out: co.Overview, lane: str, host: str) -> str:
    row = next(r for r in out["lanes"] if r["lane"] == lane)
    return next(c["state"] for c in row["cells"] if c["host"] == host)


def test_flag_off_lane_reads_off_and_is_not_a_need():
    out = co.build(_inputs(loops={"studio": {"lanes": {**ALL_ON, "cluster": False}, "loops": {}}}), NOW)
    assert _cell(out, "cluster", "studio") == "off"
    assert out["needs"] == []


def test_tripped_lane_is_a_need_unless_stood_down():
    tripped = {"studio": {"design": {"at": _iso(120), "kind": "coverage", "reason": "r"}}}
    out = co.build(_inputs(tripped=tripped), NOW)
    assert _cell(out, "design", "studio") == "tripped"
    [need] = out["needs"]
    assert need["kind"] == "trip" and need["key"].startswith("trip:studio:design:")
    out = co.build(_inputs(tripped=tripped, stood_down={"studio": {"at": _iso(1)}}), NOW)
    assert _cell(out, "design", "studio") == "stood-down" and out["needs"] == []


def test_offline_machine_is_a_need_unless_stood_down():
    roster = {"local": "studio", "machines": [_machine("studio", 0.5), _machine("lap", 300)]}
    loops = {"studio": {"lanes": ALL_ON, "loops": {}}, "lap": {"lanes": ALL_ON, "loops": {}}}
    out = co.build(_inputs(roster=roster, loops=loops), NOW)
    assert "lap" in [n["host"] for n in out["needs"] if n["kind"] == "offline"]
    assert next(m for m in out["machines"] if m["host"] == "lap")["state"] == "offline"
    out = co.build(_inputs(roster=roster, loops=loops, stood_down={"lap": {"at": _iso(400)}}), NOW)
    assert not [n for n in out["needs"] if n["kind"] == "offline"]
    lap = next(m for m in out["machines"] if m["host"] == "lap")
    assert lap["state"] == "off" and lap["stood_down"] is True


def test_lanes_fall_back_to_beats_without_loops_row():
    roster = {"local": "studio", "machines": [_machine(
        "old", 0.5, fix={"last_beat": _iso(0.5), "online": True,
                         "current_pr": None, "current_issue": None})]}
    out = co.build(_inputs(roster=roster, loops={}), NOW)
    assert _cell(out, "autofix", "old") == "on"
    assert _cell(out, "verify", "old") == "off"


def test_ai_paused_lane_state():
    out = co.build(_inputs(ai_paused={"acct": {"reason": "weekly", "retry_at": _iso(-60)}}), NOW)
    assert _cell(out, "autofix", "studio") == "ai-paused"
    assert out["ai_paused"] is not None and out["ai_paused"]["reason"] == "weekly"


def test_loop_next_run_is_last_plus_interval():
    loops = {"studio": {"lanes": ALL_ON, "loops": {
        "pr-watch": {"last_at": _iso(9), "every_seconds": 900}}}}
    [loop] = co.build(_inputs(loops=loops), NOW)["loops"]
    assert loop["next_at"] == _iso(9 - 15)


def test_loops_on_offline_host_have_no_next():
    roster = {"local": "studio", "machines": [_machine("studio", 300)]}
    loops = {"studio": {"lanes": ALL_ON, "loops": {
        "pr-watch": {"last_at": _iso(9), "every_seconds": 900}}}}
    [loop] = co.build(_inputs(roster=roster, loops=loops), NOW)["loops"]
    assert loop["next_at"] is None


def test_queue_head_and_now():
    vq = [{"pr": 7, "title": "a", "status": "running", "source": "auto", "step": "green",
           "queued_at": _iso(10), "started_at": _iso(3), "host": "studio"},
          {"pr": 8, "title": "b", "status": "queued", "source": "operator", "step": None,
           "queued_at": _iso(5), "started_at": None, "host": None}]
    out = co.build(_inputs(verify_queue=vq, pools={"security": 4, "verify": 2, "design": 0}), NOW)
    row = next(r for r in out["lanes"] if r["lane"] == "verify")
    assert [n["pr"] for n in row["now"]] == [7]
    assert [q["pr"] for q in row["queue"]] == [8]
    sec = next(r for r in out["lanes"] if r["lane"] == "security")
    assert sec["pool"] == 4 and sec["queue"] == []


def test_parked_changes_are_one_need():
    fq = [{"pr": p, "title": None, "status": "awaiting-review", "action": "fix", "source": "auto",
           "step": None, "queued_at": _iso(60 * p), "started_at": None, "host": None}
          for p in (1, 2)]
    [need] = co.build(_inputs(fix_queue=fq), NOW)["needs"]
    assert need["kind"] == "parked" and need["count"] == 2 and need["at"] == _iso(120)


def test_timeline_lists_endings_and_collapses_background():
    rows = [("pr", {"phase": "security:review-one", "pr": 5, "host": "studio",
                    "finished": _iso(20), "stats": {"verdict": "GREEN"}}),
            ("pr", {"phase": "fix:single", "pr": 6, "host": "studio", "finished": _iso(30),
                    "stats": {"status": "failed", "kind": "sandbox"}}),
            ("pr", {"phase": "ingest:watch", "host": "studio", "finished": _iso(40)}),
            ("pr", {"phase": "ingest:watch", "host": "studio", "finished": _iso(45)})]
    past = [t for t in co.build(_inputs(rows=rows), NOW)["timeline"] if t["when"] == "past"]
    assert [(t["lane"], t["pr"], t["bad"]) for t in past[:2]] == [
        ("security", 5, False), ("autofix", 6, True)]
    watch = [t for t in past if t["lane"] == "background"]
    assert len(watch) == 1 and watch[0]["text"] == "PR watch ×2"
    assert watch[0]["hint"] == co.machine_activity.BACKGROUND_HINTS["ingest:watch"]
    row = next(r for r in co.build(_inputs(rows=rows), NOW)["lanes"] if r["lane"] == "autofix")
    assert row["ended_24h"] == 1 and row["faults_24h"] == 1


def test_loop_of_a_lane_turned_off_has_no_next():
    loops = {"studio": {"lanes": {**ALL_ON, "cluster": False}, "loops": {
        "cluster": {"last_at": _iso(10), "every_seconds": 3600}}}}
    [loop] = co.build(_inputs(loops=loops), NOW)["loops"]
    assert loop["next_at"] is None


def test_overdue_loop_has_no_next():
    loops = {"studio": {"lanes": ALL_ON, "loops": {
        "pr-watch": {"last_at": _iso(180), "every_seconds": 900}}}}
    [loop] = co.build(_inputs(loops=loops), NOW)["loops"]
    assert loop["next_at"] is None
    assert not [t for t in co.build(_inputs(loops=loops), NOW)["timeline"] if t["when"] == "upcoming"]


def test_trip_need_carries_remedy_and_last_self_test():
    tripped = {"studio": {"verify": {"at": _iso(5), "kind": "sandbox", "reason": "mount broke",
                                     "remedy": "restart colima", "retest": "docker unreachable"}}}
    [need] = co.build(_inputs(tripped=tripped), NOW)["needs"]
    assert need["detail"] is not None
    assert "mount broke" in need["detail"] and "restart colima" in need["detail"]
    assert "docker unreachable" in need["detail"]


def test_need_keys_change_when_their_content_does():
    def parked(prs):
        return [{"pr": p, "title": None, "status": "awaiting-review", "action": "fix", "source": "auto",
                 "step": None, "queued_at": _iso(60), "started_at": None, "host": None} for p in prs]
    key = lambda fq: co.build(_inputs(fix_queue=fq), NOW)["needs"][0]["key"]  # noqa: E731
    assert key(parked([1, 2])) == key(parked([2, 1]))
    assert key(parked([1, 2])) != key(parked([1, 2, 3]))
    runs = lambda groups: co.build(_inputs(failed_runs=groups), NOW)["needs"][0]["key"]  # noqa: E731
    assert runs([{"lane": "verify", "reason": "a", "prs": [1]}]) != runs(
        [{"lane": "verify", "reason": "a", "prs": [1]}, {"lane": "verify", "reason": "b", "prs": [2]}])
    trip = lambda at: co.build(_inputs(tripped={"studio": {"design": {"at": at, "kind": "k"}}}),  # noqa: E731
                               NOW)["needs"][0]["key"]
    assert trip(_iso(5)) != trip(_iso(1))


def test_stale_beat_with_a_current_pr_is_not_working():
    roster = {"local": "studio", "machines": [{
        "host": "studio", "online": True, "ai_account": None, "beats": {
            "verify": {"last_beat": _iso(0.5), "online": True, "current_pr": None, "current_issue": None},
            "fix": {"last_beat": _iso(300), "online": False, "current_pr": 9, "current_issue": None}}}]}
    [m] = co.build(_inputs(roster=roster), NOW)["machines"]
    assert m["state"] == "idle"


def test_gather_survives_a_failing_source_and_caches(monkeypatch, temp_store):
    from prospector_app.backend import capacity_view
    def boom():
        raise RuntimeError("capacity read failed")
    monkeypatch.setattr(capacity_view, "accounts", boom)
    calls = []
    real = co.build
    monkeypatch.setattr(co, "build", lambda inp, now: calls.append(1) or real(inp, now))
    monkeypatch.setattr(co, "_cache", None)
    first = co.gather()
    assert first["ai_paused"] is None
    assert co.gather() is first and len(calls) == 1


def test_paused_lane_reads_paused_and_is_not_a_need():
    out = co.build(_inputs(paused={"studio": {"fix"}}), NOW)
    assert _cell(out, "autofix", "studio") == "paused"
    assert _cell(out, "verify", "studio") == "on"
    assert out["needs"] == []
    assert out["machines"][0]["lanes_on"] == len(co.LANES) - 1


def test_a_paused_tripped_lane_reads_paused():
    tripped = {"studio": {"fix": {"at": _iso(5), "kind": "crash"}}}
    out = co.build(_inputs(paused={"studio": {"fix"}}, tripped=tripped), NOW)
    assert _cell(out, "autofix", "studio") == "paused"
    assert out["needs"] == []


def test_picks_and_priority_reach_their_lane():
    picks = {"security": [{"pr": 3, "title": "t", "action": None}]}
    vq = [{"pr": 8, "title": "b", "status": "queued", "source": "operator", "step": None,
           "queued_at": _iso(5), "started_at": None, "host": None, "priority": 2.0}]
    out = co.build(_inputs(picks=picks, verify_queue=vq), NOW)
    sec = next(r for r in out["lanes"] if r["lane"] == "security")
    assert sec["picks"] == picks["security"]
    ver = next(r for r in out["lanes"] if r["lane"] == "verify")
    assert ver["picks"] == [] and ver["queue"][0]["priority"] == 2.0


def test_a_queue_head_reads_as_its_number_then_next():
    vq = [{"pr": 8, "title": "b", "status": "queued", "source": "operator", "step": None,
           "queued_at": _iso(5), "started_at": None, "host": None}]
    [head] = [t for t in co.build(_inputs(verify_queue=vq), NOW)["timeline"] if t["when"] == "upcoming"]
    assert head["text"] == "#8 · first in its queue"


def test_a_fresh_gather_skips_the_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(co, "_gather", lambda: calls.append(1) or {"n": len(calls)})
    monkeypatch.setattr(co, "_cache", None)
    co.gather()
    co.gather()
    assert len(calls) == 1
    co.gather(fresh=True)
    assert len(calls) == 2


def test_every_loop_and_background_phase_has_a_hint():
    assert set(co.worker_loops.HINTS) == set(co.worker_loops.LABELS)
    assert set(co.machine_activity.BACKGROUND_HINTS) == set(co.machine_activity.BACKGROUND)


def test_upcoming_loop_carries_its_hint():
    loops = {"studio": {"lanes": ALL_ON, "loops": {
        "pr-watch": {"last_at": _iso(10), "every_seconds": 900}}}}
    out = co.build(_inputs(loops=loops), NOW)
    [loop] = out["loops"]
    [up] = [t for t in out["timeline"] if t["when"] == "upcoming"]
    assert loop["hint"] == up["hint"] == co.worker_loops.HINTS["pr-watch"]
