"""Backend test fixtures: pin the review profile, reset the in-memory data
snapshot between tests, and provide a throwaway SQLite store."""
from __future__ import annotations

import tempfile

import pytest

from pipeline import settings


@pytest.fixture(autouse=True)
def _greptile_profile(monkeypatch):
    """These tests were written against the greptile deployment (Greptile columns,
    filters, checks). Pin the greptile review profile so they assert that surface
    regardless of the `none` default; no-provider tests override explicitly."""
    monkeypatch.setattr(settings, "REVIEW_PROVIDER", "greptile")
    monkeypatch.setattr(settings, "REVIEW_THRESHOLD", None)


@pytest.fixture(autouse=True)
def _cold_data_snapshot(monkeypatch):
    """Reset data's in-memory store snapshot before each test, so one test's
    monkeypatched store/overlay never leaks into the next via the module-level
    cache. A test that wants data populated loads it (monkeypatch _store + refresh,
    or monkeypatch data.prs/clusters directly)."""
    from prospector_app.backend import data
    for attr, val in (("_prs", {}), ("_clusters", {}), ("_pr_to_clusters_idx", {}),
                      ("_pr_watermark", None), ("_clu_watermark", None),
                      ("_loaded", False), ("_last_check", 0.0)):
        monkeypatch.setattr(data, attr, val)


@pytest.fixture
def temp_store(monkeypatch):
    """Point the shared store engine at a throwaway SQLite DB so activity/chat
    tests never touch the real store. Returns the URL; activity._engine() and
    chat._engine() resolve to this DB via settings.STORE_URL."""
    from pipeline import schema
    from pipeline import storekit
    url = f"sqlite:///{tempfile.mkdtemp()}/t.db"
    monkeypatch.setattr("pipeline.settings.STORE_URL", url)
    storekit.get_engine(url)  # warm the cache
    schema.METADATA.create_all(storekit.get_engine(url))
    return url
