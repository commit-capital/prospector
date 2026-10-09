"""Store accessors behind the AI capacity governor: the per-account policy row,
the shared reading/pause row, and background cost from the agent ledger."""
import pytest

from pipeline.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def test_an_account_policy_round_trips(store):
    assert store.load_ai_account("k") is None
    store.save_ai_account("k", {"label": "pa…@example.com · Max", "policy": {"day_cap": 0.5}})
    assert store.load_ai_account("k")["policy"]["day_cap"] == 0.5


def test_a_missing_capacity_row_reads_empty(store):
    assert store.load_capacity("k") == {}


def test_older_reading_never_replaces_a_newer_one(store):
    store.save_capacity_reading_if_newer(
        "k", {"at": "2026-10-01T10:00:00+00:00", "five_hour": {"utilization": 0.4}})
    store.save_capacity_reading_if_newer(
        "k", {"at": "2026-10-01T09:59:00+00:00", "five_hour": {"utilization": 0.9}})
    assert store.load_capacity("k")["reading"]["five_hour"]["utilization"] == 0.4
    store.save_capacity_reading_if_newer(
        "k", {"at": "2026-10-01T10:05:00+00:00", "five_hour": {"utilization": 0.7}})
    assert store.load_capacity("k")["reading"]["five_hour"]["utilization"] == 0.7


def test_a_pause_and_a_reading_are_kept_independently(store):
    store.save_capacity_reading_if_newer("k", {"at": "2026-10-01T10:00:00+00:00"})
    store.save_capacity_pause_if_newer("k", {"at": "2026-10-01T10:01:00+00:00",
                                             "until": "2026-10-01T15:00:00+00:00",
                                             "reason": "limit"})
    store.save_capacity_reading_if_newer("k", {"at": "2026-10-01T10:02:00+00:00"})
    row = store.load_capacity("k")
    assert row["reading"]["at"] == "2026-10-01T10:02:00+00:00"
    assert row["pause"]["reason"] == "limit"


def test_an_older_pause_never_replaces_a_newer_one(store):
    store.save_capacity_pause_if_newer("k", {"at": "2026-10-01T10:05:00+00:00",
                                             "until": "2026-10-01T15:00:00+00:00", "reason": "new"})
    store.save_capacity_pause_if_newer("k", {"at": "2026-10-01T10:00:00+00:00",
                                             "until": "2026-10-01T12:00:00+00:00", "reason": "old"})
    assert store.load_capacity("k")["pause"]["reason"] == "new"


def test_capacity_cost_sums_one_accounts_unattended_runs_since_a_bound(store):
    for acct, unattended, cost, ts in (("k", True, 1.5, "2026-10-01T10:00:00+00:00"),
                                       ("k", False, 9.0, "2026-10-01T10:00:00+00:00"),
                                       ("other", True, 4.0, "2026-10-01T10:00:00+00:00"),
                                       ("k", True, 0.5, "2026-10-01T11:00:00+00:00"),
                                       ("k", True, 7.0, "2026-09-30T23:00:00+00:00")):
        store.append_agent_run({"phase": "agent:run", "account": acct, "unattended": unattended,
                                "cost_usd": cost, "started": ts, "finished": ts, "ts": ts})
    assert store.capacity_cost("k", "2026-10-01T00:00:00+00:00") == 2.0


def test_agent_runs_stay_out_of_the_pr_ledger(store):
    store.append_agent_run({"phase": "agent:run", "account": "k", "unattended": True,
                            "cost_usd": 1.0, "started": "2026-10-01T10:00:00+00:00",
                            "finished": "2026-10-01T10:00:00+00:00"})
    assert store.runs() == []


def _agent_run(store, ts: str, **fields) -> None:
    store.append_agent_run({"phase": "agent:run", "started": ts, "finished": ts, "ts": ts,
                            **fields})


def test_agent_cost_sums_per_group_in_a_window(store):
    _agent_run(store, "2026-10-01T10:00:00+00:00", lane="fix", host="a", cost_usd=1.5)
    _agent_run(store, "2026-10-01T11:00:00+00:00", lane="fix", host="b", cost_usd=0.5)
    _agent_run(store, "2026-10-01T12:00:00+00:00", lane="verify", host="a", cost_usd=2.0)
    _agent_run(store, "2026-10-01T12:30:00+00:00", host="a", cost_usd=0.25)
    _agent_run(store, "2026-10-01T13:00:00+00:00", lane="fix", host="a", cost_usd=None)
    _agent_run(store, "2026-10-02T09:00:00+00:00", lane="fix", host="a", cost_usd=8.0)
    _agent_run(store, "2026-09-30T23:00:00+00:00", lane="fix", host="a", cost_usd=7.0)
    since, until = "2026-10-01T00:00:00+00:00", "2026-10-01T23:59:59+00:00"
    assert store.agent_cost(since, "lane", until=until) == {"fix": 2.0, "verify": 2.0, None: 0.25}
    assert store.agent_cost(since, "host", until=until) == {"a": 3.75, "b": 0.5}
    assert store.agent_cost(since, "host") == {"a": 11.75, "b": 0.5}


def test_runs_keeps_only_the_named_phase_server_side(store):
    store.append_run({"phase": "ingest", "ts": "2026-10-01T10:00:00+00:00"})
    store.append_run({"phase": "fix:single", "pr": 1, "ts": "2026-10-01T11:00:00+00:00"})
    store.append_run({"phase": "fix:single", "pr": 2, "ts": "2026-09-30T11:00:00+00:00"})
    got = store.runs(since="2026-10-01T00:00:00+00:00", phase="fix:single")
    assert [r.raw.get("pr") for r in got] == [1]
