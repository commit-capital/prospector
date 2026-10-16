"""system_health: the strip's summary — lanes down across machines, stale
ingests, and the stalled verdict Home's workers column reads."""
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import store as S
from pipeline import worker_health
from prospector_app.backend import data, system_health, threat_view


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def _tripped_host(host: str, lanes: list[str], kind: str = "agent-unavailable",
                  reason: str = "claude CLI is not authenticated") -> dict:
    return {"host": host,
            "lanes": {n: {"tripped": {"at": _iso(1), "kind": kind, "reason": reason}}
                      for n in lanes},
            "tripped": sorted(lanes)}


FRESH = {"pr": _iso(1), "issues": _iso(2), "alerts": _iso(3)}


def test_all_healthy_reads_ok():
    out = system_health.summarize(
        [], [], [("security", "mac"), ("verify", "mac"), ("fix", "mac")],
        FRESH, NOW_TS)
    assert out == {"severity": "ok", "items": [], "lanes_total": 3,
                   "lanes_down": 0, "workers_stalled": False}


def test_one_tripped_lane_among_several_is_amber_not_stalled():
    out = system_health.summarize(
        [_tripped_host("mac", ["fix"])], [],
        [("security", "linux"), ("verify", "linux"), ("fix", "mac")],
        FRESH, NOW_TS)
    assert out["severity"] == "amber"
    assert (out["lanes_total"], out["lanes_down"]) == (3, 1)
    assert not out["workers_stalled"]
    labels = [i["label"] for i in out["items"]]
    assert "1/3 worker lanes down" in labels
    assert "mac: fix paused · agent-unavailable" in labels
    trip = next(i for i in out["items"] if i["kind"] == "trip")
    assert trip["detail"] == "claude CLI is not authenticated"


def test_every_lane_down_is_red_and_stalled():
    out = system_health.summarize(
        [_tripped_host("mac", ["security", "verify", "fix"])], [],
        [("security", "mac"), ("verify", "mac"), ("fix", "mac")],
        FRESH, NOW_TS)
    assert out["severity"] == "red"
    assert (out["lanes_total"], out["lanes_down"]) == (3, 3)
    assert out["workers_stalled"]
    assert out["items"][0]["label"] == "3/3 worker lanes down"


def test_an_offline_worker_counts_its_lanes_down():
    offline = [{"host": "mac", "lane": "verify", "last_beat": _iso(3),
                "age_seconds": 3 * 3600.0}]
    out = system_health.summarize(
        [], offline,
        [("security", "mac"), ("verify", "mac"), ("fix", "linux")],
        FRESH, NOW_TS)
    assert out["severity"] == "amber"
    assert (out["lanes_total"], out["lanes_down"]) == (3, 2)
    assert "mac: worker offline · silent 3h" in [i["label"] for i in out["items"]]


def test_an_offline_host_shows_its_silence_not_its_trips():
    offline = [{"host": "mac", "lane": "fix", "last_beat": _iso(2),
                "age_seconds": 2 * 3600.0}]
    out = system_health.summarize(
        [_tripped_host("mac", ["fix"])], offline, [("fix", "mac")],
        FRESH, NOW_TS)
    kinds = [i["kind"] for i in out["items"]]
    assert "offline" in kinds and "trip" not in kinds
    assert out["workers_stalled"]


def test_a_trip_on_a_host_the_registries_dropped_still_counts():
    out = system_health.summarize(
        [_tripped_host("gone", ["fix"])], [], [], FRESH, NOW_TS)
    assert (out["lanes_total"], out["lanes_down"]) == (1, 1)
    assert out["severity"] == "red"
    assert out["workers_stalled"]


def test_stale_ingest_turns_amber_then_red():
    out = system_health.summarize([], [], [], {"pr": _iso(30)}, NOW_TS)
    assert out["severity"] == "amber"
    assert out["items"] == [{"kind": "ingest", "severity": "amber", "host": None,
                             "label": "PR ingest 30h old",
                             "detail": f"last ran {_iso(30)}"}]

    out = system_health.summarize([], [], [], {"alerts": _iso(19 * 24)}, NOW_TS)
    assert out["severity"] == "red"
    assert out["items"][0]["label"] == "alert ingest 19d old"
    assert not out["workers_stalled"]


def test_an_ingest_that_never_ran_reports_nothing():
    out = system_health.summarize(
        [], [], [], {"pr": None, "issues": None, "alerts": None}, NOW_TS)
    assert out == {"severity": "ok", "items": [], "lanes_total": 0,
                   "lanes_down": 0, "workers_stalled": False}


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    data.refresh()
    monkeypatch.setattr(system_health, "_pr_ingest_cache", None)
    monkeypatch.setattr(threat_view, "_registry_cache", None)
    return st


def test_status_carries_the_open_malicious_prs_and_secrets_to_rotate(store, monkeypatch):
    from pipeline import actions
    from prospector_app.backend import alert_data, issues
    monkeypatch.setattr(issues, "cached_runs", lambda: [])
    monkeypatch.setattr(alert_data, "runs", lambda: [])
    store.save_pr({"pr": 11987,
                   "meta": {"title": "t", "author": "mallory", "state": "open",
                            "head_sha": "h" * 40},
                   "threat": {"verdict": "malicious", "signatures": ["obfuscated-payload"],
                              "detail": {}, "checked_at": _iso(1), "against_head_sha": "h" * 40}})
    store.save_pr({"pr": 12001,
                   "meta": {"title": "t", "author": "dotta", "author_association": "MEMBER",
                            "state": "open", "head_sha": "m" * 40}})
    reg = actions.empty_registry()
    for n in (11987, 12001):
        actions.upsert(reg, actions.make_item("rotate-secret", pr=n, summary="leak",
                                              created="2026-10-01", fixture=False))
    store.save_action_items(reg)
    data.refresh()
    out = system_health.status()
    assert [m["pr"] for m in out["threats"]["malicious"]] == [11987]
    assert out["threats"]["secrets"] == [12001]


def test_status_composes_the_live_inputs(store, monkeypatch):
    from prospector_app.backend import alert_data, issues
    monkeypatch.setattr(issues, "cached_runs", lambda: [])
    monkeypatch.setattr(alert_data, "runs", lambda: [])
    now = datetime.now(timezone.utc)
    store.append_run({"phase": "ingest", "started": now.isoformat(),
                      "finished": now.isoformat(), "stats": {}})
    store.save_fix_worker({"host": "mac", "last_beat": now.isoformat()})
    worker_health.update(store, "mac", lambda r: worker_health.trip(
        r, "fix", kind="agent-unavailable", reason="CLI missing"))

    out = system_health.status()
    assert out["severity"] == "red"
    assert (out["lanes_total"], out["lanes_down"]) == (1, 1)
    assert out["workers_stalled"]
    assert [i["kind"] for i in out["items"]] == ["lanes", "trip"]


def test_status_counts_the_issue_fix_lane(store, monkeypatch):
    from prospector_app.backend import alert_data, issues
    monkeypatch.setattr(issues, "cached_runs", lambda: [])
    monkeypatch.setattr(alert_data, "runs", lambda: [])
    now = datetime.now(timezone.utc)
    store.save_fix_worker({"host": "mac", "last_beat": now.isoformat()})
    store.save_issue_fix_worker({"host": "mac", "last_beat": now.isoformat()})
    worker_health.update(store, "mac", lambda r: worker_health.trip(
        r, "fix", kind="crash", reason="crashed"))

    out = system_health.status()
    assert (out["lanes_total"], out["lanes_down"]) == (2, 1)
    assert not out["workers_stalled"]


def test_trip_detail_carries_the_remedy():
    host = _tripped_host("mac", ["fix"])
    host["lanes"]["fix"]["remedy"] = "sign in with /login"
    out = system_health.summarize([host], [], [("fix", "mac")], FRESH, NOW_TS)
    trip = next(i for i in out["items"] if i["kind"] == "trip")
    assert trip["detail"] == "claude CLI is not authenticated\nsign in with /login"


def test_status_reads_every_worker_registry_in_one_statement(store, monkeypatch):
    from sqlalchemy import event

    from prospector_app.backend import alert_data, capacity_view, issues
    monkeypatch.setattr(issues, "cached_runs", lambda: [])
    monkeypatch.setattr(alert_data, "runs", lambda: [])
    monkeypatch.setattr(capacity_view, "health_items", lambda: [])
    monkeypatch.setattr(threat_view, "summary",
                        lambda: {"malicious": [], "suspicious": 0, "secrets": 0})
    now = datetime.now(timezone.utc)
    store.save_fix_worker({"host": "mac", "last_beat": now.isoformat()})
    store.save_verify_worker({"host": "linux", "last_beat": (now - timedelta(hours=3)).isoformat()})
    worker_health.update(store, "mac", lambda r: worker_health.trip(
        r, "fix", kind="crash", reason="crashed"))
    statements: list[str] = []

    def count(conn, cursor, statement, *rest) -> None:
        statements.append(statement)

    event.listen(store.engine, "before_cursor_execute", count)
    try:
        out = system_health.status()
    finally:
        event.remove(store.engine, "before_cursor_execute", count)
    assert sum("FROM registries" in s for s in statements) == 1
    assert (out["lanes_total"], out["lanes_down"]) == (3, 3)
    assert [i["kind"] for i in out["items"]] == ["lanes", "offline", "trip"]


def test_stood_down_host_drops_out_of_strip():
    out = system_health.summarize(
        [_tripped_host("lap", ["fix"])],
        [{"host": "lap", "lane": "fix", "last_beat": _iso(5), "age_seconds": 5 * 3600}],
        [("fix", "lap"), ("verify", "mac")], FRESH, NOW_TS,
        stood_down=frozenset({"lap"}))
    assert out["severity"] == "ok" and out["items"] == []
    assert out["lanes_total"] == 1 and out["lanes_down"] == 0


def test_other_hosts_still_report_when_one_is_stood_down():
    out = system_health.summarize(
        [_tripped_host("mac", ["verify"])], [],
        [("verify", "mac"), ("fix", "lap")], FRESH, NOW_TS,
        stood_down=frozenset({"lap"}))
    assert [i["kind"] for i in out["items"]] == ["lanes", "trip"]
    assert out["lanes_total"] == 1 and out["workers_stalled"] is True


GB = 10**9


def _disk_host(host: str, disk: dict) -> dict:
    return {"host": host, "lanes": {}, "tripped": [], "disk": disk}


def test_a_filling_docker_disk_warns_naming_the_machine_and_figures():
    disk = {"at": _iso(0.1), "free_bytes": 12 * GB, "total_bytes": 158 * GB,
            "min_free_bytes": 20 * GB}
    out = system_health.summarize([_disk_host("studio", disk)], [], [("verify", "studio")],
                                  FRESH, NOW_TS)
    assert out["severity"] == "amber"
    [item] = out["items"]
    assert (item["kind"], item["host"]) == ("disk", "studio")
    assert item["label"] == "studio: Docker disk 12 GB free of 158 GB"
    assert "verify_driver.py gc" in item["detail"] and "docker system df" in item["detail"]
    assert out["lanes_down"] == 0


def test_docker_disk_above_its_floor_reads_ok():
    disk = {"at": _iso(0.1), "free_bytes": 77 * GB, "total_bytes": 158 * GB,
            "min_free_bytes": 20 * GB}
    out = system_health.summarize([_disk_host("studio", disk)], [], [], FRESH, NOW_TS)
    assert out["items"] == []


def test_a_full_docker_disk_is_red():
    disk = {"at": _iso(0.1), "error": "docker: Error response from daemon: "
                                      "mkdir /var/lib/docker/x: no space left on device"}
    out = system_health.summarize([_disk_host("studio", disk)], [], [], FRESH, NOW_TS)
    assert out["severity"] == "red"
    assert out["items"][0]["label"] == "studio: Docker disk full"


def test_an_old_or_unreadable_disk_reading_reports_nothing():
    old = {"at": _iso(2), "free_bytes": 1 * GB, "total_bytes": 158 * GB,
           "min_free_bytes": 20 * GB}
    unreadable = {"at": _iso(0.1), "error": "no sandbox image to measure with"}
    out = system_health.summarize([_disk_host("a", old), _disk_host("b", unreadable)],
                                  [], [], FRESH, NOW_TS)
    assert out["items"] == []


def test_status_reads_the_recorded_docker_disk(store, monkeypatch):
    from prospector_app.backend import alert_data, issues
    monkeypatch.setattr(issues, "cached_runs", lambda: [])
    monkeypatch.setattr(alert_data, "runs", lambda: [])
    worker_health.update(store, "studio", lambda r: worker_health.record_disk(
        r, {"free_bytes": 5 * GB, "total_bytes": 158 * GB}, min_free_bytes=20 * GB))

    out = system_health.status()
    assert [i["kind"] for i in out["items"]] == ["disk"]


def test_a_machine_behind_the_store_reads_needs_updating_not_offline():
    out = system_health.summarize(
        [], [{"host": "studio", "lane": "verify", "age_seconds": 7200.0}],
        [("verify", "studio"), ("fix", "laptop")], FRESH, NOW_TS,
        outdated={"studio": 32}, store_schema=33, me="laptop")
    assert out["severity"] == "red"
    assert out["lanes_down"] == 1
    labels = [i["label"] for i in out["items"]]
    assert "studio: needs updating · on v32, store is v33" in labels
    assert not any("offline" in label for label in labels)


def test_this_machine_behind_the_store_asks_for_an_update_first():
    out = system_health.summarize(
        [], [], [("verify", "studio"), ("verify", "laptop")], FRESH, NOW_TS,
        outdated={"studio": 32, "laptop": 32}, store_schema=33, me="studio")
    first = out["items"][0]
    assert first["kind"] == "outdated" and first["host"] == "studio"
    assert first["label"] == ("Action needed: update Prospector on this machine — "
                              "the store is on v33, this Prospector is v32")
    assert out["workers_stalled"] is True
