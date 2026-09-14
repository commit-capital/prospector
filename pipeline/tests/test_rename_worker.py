"""rename_worker: folding one worker name into another across PR host stamps
and the three host-keyed registries."""
import pytest

from pipeline import rename_worker
from pipeline.store import Store

OLD, NEW = "Laptop.local", "laptop"


def _pin(host, when):
    return {"host": host, "base_sha": "b" * 40, "tier": 1, "pinned_at": when,
            "baseline_failing": [], "baseline_captured_at": when}


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "db")
    for n in (1, 2, 3, 4):
        st.save_pr({"pr": n, "meta": {"title": f"pr {n}", "state": "open",
                                       "head_sha": f"h{n}", "body": "",
                                       "checked_at": "2026-09-01T00:00:00+00:00"}})
    st.edit_pr(1).record_fix_request("awaiting-review", "resolve", host=OLD,
                                      head_sha="h1", result={"auto_review": {"host": OLD}})
    st.edit_pr(2).record_verify_request("error", host=OLD, error_kind="exception")
    st.edit_pr(3).record_fix_request("queued", "update", host="other")
    st.save_verify_base(_pin(OLD, "2026-09-10T00:00:00+00:00"))
    st.save_verify_base(_pin(NEW, "2026-09-14T00:00:00+00:00"))
    st.save_verify_worker({"host": OLD, "last_beat": "2026-09-10T00:00:00+00:00"})
    st.save_fix_worker({"host": OLD, "last_beat": "2026-09-10T00:00:00+00:00"})
    return st


def test_rename_record_touches_only_stamps_of_the_old_name(store):
    out = rename_worker.rename_record(store.load_pr(1).raw, OLD, NEW)
    assert out["fix_request"]["host"] == NEW
    assert out["fix_request"]["result"]["auto_review"]["host"] == NEW
    assert rename_worker.rename_record(store.load_pr(3).raw, OLD, NEW) is None
    assert rename_worker.rename_record(store.load_pr(4).raw, OLD, NEW) is None


def test_dry_run_changes_nothing(store, capsys):
    rename_worker.main([OLD, NEW, "--store", str(store.root)])
    assert store.load_pr(1).fix_request["host"] == OLD
    assert OLD in store.load_verify_base_hosts()
    assert "dry run" in capsys.readouterr().out


def test_live_folds_records_and_registries(store):
    rename_worker.main([OLD, NEW, "--store", str(store.root), "--live"])
    assert store.load_pr(1).fix_request["host"] == NEW
    assert store.load_pr(2).verify_request["host"] == NEW
    assert store.load_pr(3).fix_request["host"] == "other"
    pins = store.load_verify_base_hosts()
    assert OLD not in pins
    # Both names held a pin; the newer one stays.
    assert pins[NEW]["pinned_at"] == "2026-09-14T00:00:00+00:00"
    assert set(store.load_verify_worker()["hosts"]) == {NEW}
    assert set(store.load_fix_worker()["hosts"]) == {NEW}
    edits = [r for r in store.runs() if getattr(r, "transform", "") == "rename_worker"]
    assert edits and edits[0].changed == [1, 2]
