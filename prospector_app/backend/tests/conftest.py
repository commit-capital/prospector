"""Backend test fixtures: pin the review profile, reset the in-memory data
snapshot between tests, and provide a throwaway SQLite store."""
from __future__ import annotations

import tempfile
import time

import pytest



@pytest.fixture(autouse=True)
def _greptile_profile(monkeypatch):
    """These tests were written against the greptile deployment (Greptile columns,
    filters, checks). Pin the greptile review profile so they assert that surface
    regardless of the `none` default; no-provider tests override explicitly."""
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    monkeypatch.delenv("TRIAGE_REVIEW_THRESHOLD", raising=False)


@pytest.fixture(autouse=True)
def _cold_data_snapshot(monkeypatch):
    """Reset data's in-memory store snapshot before each test, so one test's
    monkeypatched store/overlay never leaks into the next via the module-level
    cache. A test that wants data populated loads it (monkeypatch _store + refresh,
    or monkeypatch data.prs/clusters directly)."""
    from prospector_app.backend import data
    from prospector_app.backend import service
    for attr, val in (("_prs", {}), ("_clusters", {}), ("_pr_to_clusters_idx", {}),
                      ("_pr_watermark", None), ("_clu_watermark", None),
                      ("_generation", 0), ("_loaded", False), ("_last_check", 0.0)):
        monkeypatch.setattr(data, attr, val)
    # The row cache is keyed on the snapshot's identity, so drop it with the
    # snapshot — a monkeypatched corpus must never serve another test's rows.
    monkeypatch.setattr(service, "_ROW_CACHE", {})
    monkeypatch.setattr(service, "_ROW_CACHE_KEY", None)
    yield
    # A freshen a test kicked onto a daemon thread publishes under _check_lock and
    # releases it last; let it finish so it never blocks the next test's cold load
    # or publishes into that test's snapshot.
    for _ in range(250):
        if not data._check_lock.locked():
            break
        time.sleep(0.02)


@pytest.fixture
def temp_store(monkeypatch):
    """Point the shared store engine at a throwaway SQLite DB so activity/chat
    tests never touch the real store. Returns the URL; activity._engine() and
    chat._engine() resolve to this DB via settings.store_url()."""
    from pipeline import schema
    from pipeline import storekit
    url = f"sqlite:///{tempfile.mkdtemp()}/t.db"
    monkeypatch.setenv("TRIAGE_STORE_URL", url)
    storekit.get_engine(url)  # warm the cache
    schema.METADATA.create_all(storekit.get_engine(url))
    return url
