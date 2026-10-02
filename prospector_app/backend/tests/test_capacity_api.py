"""The capacity view and its routes: every account the workers report, this
machine's own account, the policy endpoint, and the health-strip line."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pipeline import capacity, worker_health
from prospector_app.backend import app as appmod
from prospector_app.backend import capacity_view, data

MINE = capacity.Account(key="mine", billing="subscription", plan="max", label="pa…@example.com · Max")


@pytest.fixture
def store(temp_store, monkeypatch):
    from pipeline.store import Store
    st = Store()
    monkeypatch.setattr(data, "store", lambda: st)
    monkeypatch.setattr(capacity, "account", lambda refresh=False: MINE)
    monkeypatch.setattr(capacity_view, "_health_cache", None)
    return st


def _worker(st, host: str, acct: capacity.Account) -> None:
    worker_health.update(st, host, lambda r: r.__setitem__("ai_account", {
        "key": acct.key, "label": acct.label, "billing": acct.billing}))


def _reading(st, acct: capacity.Account, five: float, at: datetime | None = None) -> None:
    at = at or datetime.now(timezone.utc)
    capacity.record_reading(st, acct, capacity.Reading(
        five_hour=capacity.Window(five, at + timedelta(hours=2)),
        seven_day=capacity.Window(0.1, at + timedelta(days=5)),
        status="allowed", at=at, by="Studio"))


def test_accounts_group_the_machines_that_share_them(store):
    other = capacity.Account(key="devin", billing="subscription", plan="pro", label="de…@x.com · Pro")
    _worker(store, "Macbook", MINE)
    _worker(store, "Studio", MINE)
    _worker(store, "Devin-PC", other)
    views = {v["key"]: v for v in capacity_view.accounts()}
    assert sorted(views["mine"]["machines"]) == ["Macbook", "Studio"]
    assert views["mine"]["this_machine"] is True
    assert views["devin"]["this_machine"] is False and views["devin"]["machines"] == ["Devin-PC"]


def test_an_account_view_carries_its_reading_cap_and_decision(store):
    _reading(store, MINE, 0.3)
    (view,) = capacity_view.accounts()
    assert view["reading"]["five_hour"]["utilization"] == 0.3
    assert view["cap_now"] in (0.5, 0.9) and view["pacing_line"] is not None
    assert view["policy_saved"] is False and view["policy"]["day_cap"] == 0.5
    assert view["decision"]["allowed"] is (0.3 < view["cap_now"])


def test_the_view_never_spends_a_probe(store, monkeypatch):
    from pipeline import headless_agent
    monkeypatch.setattr(headless_agent, "probe_reading",
                        lambda timeout=180: pytest.fail("the view probed"))
    (view,) = capacity_view.accounts()
    assert view["reading"] is None and view["decision"]["allowed"] is False


def test_spend_today_is_split_by_lane(store):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for lane, cost, unattended in (("fix", 1.0, True), ("fix", 0.5, True),
                                   ("security", 2.0, True), ("operator", 9.0, False)):
        store.append_agent_run({"phase": "agent:run", "account": "mine", "lane": lane,
                                "unattended": unattended, "cost_usd": cost,
                                "started": now, "finished": now, "ts": now})
    (view,) = capacity_view.accounts()
    assert view["spend_today_by_lane"] == {"fix": 1.5, "security": 2.0}


def test_get_capacity_route(store):
    _reading(store, MINE, 0.2)
    body = TestClient(appmod.app).get("/api/capacity").json()
    assert body["this_account"] == "mine" and body["accounts"][0]["key"] == "mine"


def test_put_policy_saves_this_machines_account(store):
    client = TestClient(appmod.app)
    r = client.put("/api/capacity/policy", json={
        "timezone": "America/Los_Angeles", "day_start": "09:00", "day_end": "22:00",
        "day_cap": 0.4, "night_cap": 0.8, "weekly_pacing": True, "daily_budget_usd": None})
    assert r.status_code == 200 and r.json()["policy_saved"] is True
    assert capacity.policy(store, MINE).day_cap == 0.4


def test_put_policy_refuses_bad_input(store):
    r = TestClient(appmod.app).put("/api/capacity/policy", json={
        "timezone": "Nowhere/Land", "day_start": "09:00", "day_end": "22:00",
        "day_cap": 0.4, "night_cap": 0.8, "weekly_pacing": True})
    assert r.status_code == 400 and "timezone" in r.json()["detail"]


def test_put_policy_without_a_signed_in_cli(store, monkeypatch):
    monkeypatch.setattr(capacity, "account", lambda refresh=False: None)
    r = TestClient(appmod.app).put("/api/capacity/policy", json={})
    assert r.status_code == 409


def test_a_paused_account_with_a_worker_raises_a_health_line(store):
    _worker(store, "Studio", MINE)
    _reading(store, MINE, 0.95)
    (item,) = capacity_view.health_items()
    assert item["kind"] == "capacity" and item["severity"] == "amber"
    assert item["label"].startswith("Background AI paused · pa…@example.com · Max")


def test_an_open_account_raises_no_health_line(store):
    _worker(store, "Studio", MINE)
    _reading(store, MINE, 0.1)
    assert capacity_view.health_items() == []
