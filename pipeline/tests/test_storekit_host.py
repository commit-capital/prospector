"""Every runs-ledger row names the machine that wrote it."""
from pipeline import storekit
from pipeline.store import Store


def test_stamp_host_adds_the_worker_id(monkeypatch):
    monkeypatch.setenv("TRIAGE_WORKER_ID", "studio")
    rec = {"phase": "ingest", "started": None, "finished": None}
    assert storekit.stamp_host(rec) == {**rec, "host": "studio"}
    assert "host" not in rec


def test_stamp_host_keeps_a_named_host(monkeypatch):
    monkeypatch.setenv("TRIAGE_WORKER_ID", "studio")
    assert storekit.stamp_host({"phase": "fix:single", "host": "laptop"})["host"] == "laptop"


def test_append_run_stamps_host(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGE_WORKER_ID", "studio")
    st = Store(tmp_path)
    st.append_run({"phase": "ingest", "started": None, "finished": None})
    assert st.runs()[-1].raw["host"] == "studio"


def test_stamp_host_keeps_a_host_named_in_stats(monkeypatch):
    monkeypatch.setenv("TRIAGE_WORKER_ID", "studio")
    rec = {"phase": "fix:single", "stats": {"host": "laptop"}}
    assert storekit.stamp_host(rec) == rec
