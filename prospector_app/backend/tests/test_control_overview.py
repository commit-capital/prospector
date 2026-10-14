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
        "failed_runs": [], "rows": []}
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
    assert [n["key"] for n in out["needs"]] == ["trip:studio:design"]
    out = co.build(_inputs(tripped=tripped, stood_down={"studio": {"at": _iso(1)}}), NOW)
    assert _cell(out, "design", "studio") == "stood-down" and out["needs"] == []


def test_offline_machine_is_a_need_unless_stood_down():
    roster = {"local": "studio", "machines": [_machine("studio", 0.5), _machine("lap", 300)]}
    loops = {"studio": {"lanes": ALL_ON, "loops": {}}, "lap": {"lanes": ALL_ON, "loops": {}}}
    out = co.build(_inputs(roster=roster, loops=loops), NOW)
    assert "offline:lap" in [n["key"] for n in out["needs"]]
    assert next(m for m in out["machines"] if m["host"] == "lap")["state"] == "offline"
    out = co.build(_inputs(roster=roster, loops=loops, stood_down={"lap": {"at": _iso(400)}}), NOW)
    assert "offline:lap" not in [n["key"] for n in out["needs"]]
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
    assert need["key"] == "parked" and need["count"] == 2 and need["at"] == _iso(120)


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
    row = next(r for r in co.build(_inputs(rows=rows), NOW)["lanes"] if r["lane"] == "autofix")
    assert row["ended_24h"] == 1 and row["faults_24h"] == 1
