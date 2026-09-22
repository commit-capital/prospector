"""The global health strip: tripped lanes, silent workers, and ingest
staleness across every machine, folded into one read every page polls."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline import store as S
from pipeline import worker_health
from pipeline.storekit import now as _now
from prospector_app.backend import data
from prospector_app.backend import health_strip
from prospector_app.backend import work_status

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setattr(health_strip, "_ingest_cache", None)
    data.refresh()
    return st


def _health(hosts: list[dict]) -> dict:
    return {"hosts": hosts, "any_tripped": any(h.get("tripped") for h in hosts)}


def _tripped_host(host: str, lanes: list[str], kind: str = "agent-unavailable") -> dict:
    return {"host": host, "tripped": sorted(lanes),
            "lanes": {name: {"tripped": {"at": "2026-09-18T00:00:00+00:00",
                                         "kind": kind, "reason": "boom"}}
                      for name in lanes}}


class TestBuild:
    def test_a_healthy_deployment_reads_ok(self):
        s = health_strip.build(_health([]), [], {("studio", "verify")},
                               NOW.isoformat(), NOW)
        assert s["level"] == "ok"
        assert s["items"] == []
        assert s["lanes_total"] == 1
        assert s["lanes_down"] == 0
        assert s["stalled"] is False

    def test_a_tripped_lane_reads_amber_and_names_the_machine_and_cause(self):
        s = health_strip.build(
            _health([_tripped_host("studio", ["fix"])]), [],
            {("studio", "fix"), ("laptop", "verify"), ("laptop", "security")},
            NOW.isoformat(), NOW)
        assert s["level"] == "amber"
        assert s["lanes_down"] == 1 and s["lanes_total"] == 3
        assert s["stalled"] is False
        texts = [i["text"] for i in s["items"]]
        assert "1/3 lanes paused" in texts
        assert "fix paused on studio (agent-unavailable)" in texts
        assert all(i["link"] == "/control" for i in s["items"])

    def test_every_lane_down_reads_red_and_stalled(self):
        s = health_strip.build(
            _health([_tripped_host("studio", ["security", "verify", "fix"])]), [],
            {("studio", "security"), ("studio", "verify"), ("studio", "fix")},
            NOW.isoformat(), NOW)
        assert s["level"] == "red"
        assert s["lanes_down"] == 3 and s["lanes_total"] == 3
        assert s["stalled"] is True
        assert "3/3 lanes paused" in [i["text"] for i in s["items"]]

    def test_a_silent_worker_downs_its_lanes_and_reads_red(self):
        offline = [{"host": "studio", "lane": "verify",
                    "last_beat": "2026-09-18T00:00:00+00:00",
                    "age_seconds": 4 * 86400.0}]
        s = health_strip.build(_health([]), offline,
                               {("studio", "security"), ("studio", "verify")},
                               NOW.isoformat(), NOW)
        assert s["level"] == "red"
        assert s["lanes_down"] == 2
        assert s["stalled"] is True
        assert "no heartbeat from studio for 4d" in [i["text"] for i in s["items"]]

    def test_a_tripped_lane_on_a_host_the_registries_forgot_still_counts(self):
        s = health_strip.build(_health([_tripped_host("retired", ["verify"])]),
                               [], set(), NOW.isoformat(), NOW)
        assert s["lanes_total"] == 1 and s["lanes_down"] == 1
        assert s["stalled"] is True

    def test_a_fresh_ingest_adds_no_item(self):
        recent = (NOW - timedelta(hours=2)).isoformat()
        s = health_strip.build(_health([]), [], set(), recent, NOW)
        assert s["items"] == []
        assert s["ingest_age_hours"] == 2.0

    def test_a_stale_ingest_reads_amber_then_red(self):
        amber = health_strip.build(
            _health([]), [], set(), (NOW - timedelta(hours=30)).isoformat(), NOW)
        assert amber["level"] == "amber"
        assert "PR ingest 30h old" in [i["text"] for i in amber["items"]]
        red = health_strip.build(
            _health([]), [], set(), (NOW - timedelta(days=19)).isoformat(), NOW)
        assert red["level"] == "red"
        assert "PR ingest 19d old" in [i["text"] for i in red["items"]]

    def test_an_ingest_that_never_ran_reads_amber(self):
        s = health_strip.build(_health([]), [], set(), None, NOW)
        assert s["level"] == "amber"
        assert "PR ingest has never run" in [i["text"] for i in s["items"]]
        assert s["ingest_age_hours"] is None


class TestLanePopulation:
    def test_verify_hosts_carry_two_lanes_and_fix_hosts_one(self):
        pairs = health_strip.lane_population(
            {"hosts": {"studio": {"host": "studio"}}},
            {"hosts": {"studio": {"host": "studio"}, "laptop": {"host": "laptop"}}})
        assert pairs == {("studio", "security"), ("studio", "verify"),
                         ("studio", "fix"), ("laptop", "fix")}

    def test_empty_registries_yield_no_lanes(self):
        assert health_strip.lane_population({}, {}) == set()


class TestStrip:
    def test_work_status_carries_the_strip(self, store):
        store.append_run({"phase": "ingest", "started": _now(), "finished": _now()})
        store.save_fix_worker({"host": "studio", "pid": 1, "last_beat": _now(),
                               "current_pr": None, "autohunt": False})
        worker_health.update(store, "studio", lambda r: worker_health.trip(
            r, "fix", kind="agent-unavailable", reason="oauth expired"))
        health = work_status.now()["health"]
        assert health["level"] == "red"
        assert health["stalled"] is True
        assert "fix paused on studio (agent-unavailable)" in [
            i["text"] for i in health["items"]]

    def test_the_ingest_scan_is_cached_between_polls(self, store):
        old = (datetime.now(timezone.utc) - timedelta(days=19)).isoformat()
        store.append_run({"phase": "ingest", "started": old, "finished": old})
        assert health_strip.strip()["level"] == "red"
        store.append_run({"phase": "ingest", "started": _now(), "finished": _now()})
        assert health_strip.strip()["level"] == "red"
