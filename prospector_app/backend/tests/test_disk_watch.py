import pytest

from pipeline import store as S
from pipeline import verify_driver, worker_health
from prospector_app.backend import data, disk_watch


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    return st


def test_check_once_records_the_reading_with_this_machines_floor(store, monkeypatch):
    monkeypatch.setattr(verify_driver, "docker_disk",
                        lambda: {"free_bytes": 50, "total_bytes": 100})
    monkeypatch.setenv("TRIAGE_DOCKER_MIN_FREE_GB", "7")
    monkeypatch.setenv("TRIAGE_WORKER_ID", "studio")
    disk_watch.check_once()
    disk = worker_health.load(data.store(), "studio")["disk"]
    assert (disk["free_bytes"], disk["total_bytes"], disk["min_free_bytes"]) == (50, 100, 7 * 10**9)


def test_a_probe_that_raises_is_recorded_as_its_error(store, monkeypatch):
    def boom() -> dict[str, int]:
        raise OSError("docker not found")
    monkeypatch.setattr(verify_driver, "docker_disk", boom)
    monkeypatch.setenv("TRIAGE_WORKER_ID", "studio")
    disk_watch.check_once()
    assert worker_health.load(data.store(), "studio")["disk"]["error"] == "OSError: docker not found"
