"""Adopting a new store in the running process.

The engine and the snapshot are both built from configuration that was in
effect at import, so a configuration write that does not reset them leaves
every read serving the store the process started with.
"""
from __future__ import annotations

import json

import pytest

from pipeline import profile
from prospector_app.backend import data


@pytest.fixture(autouse=True)
def restore_snapshot():
    """`data` holds one store and one snapshot for the process, so a test that
    repoints it puts the suite's own store back before the next one reads."""
    yield
    data.reset()


def test_reset_rebuilds_the_store_against_the_current_url(tmp_path, monkeypatch):
    before = data.store()
    monkeypatch.setenv("TRIAGE_STORE_URL", f"sqlite:///{tmp_path}/other.db")
    data.reset()
    after = data.store()
    assert after is not before
    assert str(tmp_path) in str(after.engine.url)


def test_reads_after_a_reset_come_from_the_new_store(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGE_STORE_URL", f"sqlite:///{tmp_path}/fresh.db")
    data.reset()
    assert data.prs() == {}
    assert data.clusters() == {}


def test_profile_reset_cache_rereads_the_same_path(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"version": 1, "subsystems": []}))
    monkeypatch.setenv("TRIAGE_PROFILE", str(path))
    first = profile.active()
    assert [s.name for s in first.subsystems] == []
    path.write_text(json.dumps(
        {"version": 1, "subsystems": [{"name": "core", "match_terms": ["core"]}]}))
    profile.reset_cache()
    second = profile.active()
    assert [s.name for s in second.subsystems] == ["core"]
