"""The Setup page's settings report, and the sandbox sizes it shows in effect."""
from __future__ import annotations

import pytest

from pipeline import verify_driver
from prospector_app.backend import env_file, settings_panel


@pytest.fixture
def env_path(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("TRIAGE_STORE_URL=postgresql://u:sup3rsecret@h/db\n# TRIAGE_FIX_HUNT_LIMIT=9\n"
                    "TRIAGE_FIX_WORKER=1\n")
    monkeypatch.setattr(env_file, "ENV_PATH", path)
    monkeypatch.setenv("TRIAGE_STORE_URL", "postgresql://u:sup3rsecret@h/db")
    monkeypatch.setenv("TRIAGE_FIX_WORKER", "1")
    monkeypatch.delenv("TRIAGE_FIX_HUNT_LIMIT", raising=False)
    monkeypatch.setenv("TRIAGE_AGENT_MODEL", "sonnet")
    monkeypatch.setattr(verify_driver, "_vm_size", lambda: (31, 10))
    monkeypatch.delenv("TRIAGE_SANDBOX_LARGE_SLOTS", raising=False)
    monkeypatch.delenv("TRIAGE_SANDBOX_LARGE_CPUS", raising=False)
    return path


def _rows() -> dict[str, dict]:
    return {r["name"]: r for r in settings_panel.report()["settings"]}


def test_a_secret_is_reported_set_never_shown(env_path):
    row = _rows()["TRIAGE_STORE_URL"]
    assert row["value"] == "set" and "sup3rsecret" not in str(settings_panel.report())


def test_each_value_names_where_it_came_from(env_path):
    rows = _rows()
    assert rows["TRIAGE_FIX_WORKER"]["source"] == ".env"
    assert rows["TRIAGE_AGENT_MODEL"]["source"] == "environment"
    assert rows["TRIAGE_FIX_HUNT_LIMIT"]["source"] == "default"


def test_the_sandbox_sizes_in_effect_are_shown(env_path):
    rows = _rows()
    assert rows["TRIAGE_SANDBOX_LARGE_SLOTS"]["effective"] == "2"
    assert rows["TRIAGE_SANDBOX_LARGE_CPUS"]["effective"] == "3"


@pytest.mark.parametrize("vm,slots,cpus", [((16, 8), 1, 4), ((31, 10), 2, 3), ((64, 16), 4, 3),
                                           ((8, 4), 1, 2), (None, 1, 2)])
def test_large_phases_are_sized_to_the_docker_vm(monkeypatch, vm, slots, cpus):
    monkeypatch.setattr(verify_driver, "_vm_size", lambda: vm)
    monkeypatch.delenv("TRIAGE_SANDBOX_LARGE_SLOTS", raising=False)
    monkeypatch.delenv("TRIAGE_SANDBOX_LARGE_CPUS", raising=False)
    assert (verify_driver.large_slots(), verify_driver.large_cpus()) == (slots, cpus)


def test_a_named_size_wins_over_the_vm(monkeypatch):
    monkeypatch.setattr(verify_driver, "_vm_size", lambda: (64, 16))
    monkeypatch.setenv("TRIAGE_SANDBOX_LARGE_SLOTS", "1")
    monkeypatch.setenv("TRIAGE_SANDBOX_LARGE_CPUS", "2")
    assert (verify_driver.large_slots(), verify_driver.large_cpus()) == (1, 2)
