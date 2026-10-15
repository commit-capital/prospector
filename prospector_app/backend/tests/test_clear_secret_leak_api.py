"""POST /api/prs/{n}/threat/clear-secret records the operator's judgment that
the secret-leak finding at the head they reviewed is no credential."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pipeline import gates, threat_scan
from pipeline.store import Store
from pipeline.tests.test_threats import LEAKED_KEY_DIFF
from prospector_app.backend import activity
from prospector_app.backend import app as appmod
from prospector_app.backend import data


@pytest.fixture
def flagged(tmp_path, monkeypatch) -> Store:
    store = Store(tmp_path / "store")
    diffs = tmp_path / "diffs"
    diffs.mkdir()
    (diffs / "h1.diff").write_text(LEAKED_KEY_DIFF)
    store.save_pr({"pr": 1, "meta": {"title": "t", "author": "mira", "state": "open",
                                     "draft": False, "head_sha": "h1",
                                     "checked_at": "2026-10-06T00:00:00+00:00"}})
    threat_scan.scan(store, store.all_prs(), diffs, fetch=False)
    monkeypatch.setattr(data, "_store", store)
    monkeypatch.setattr(activity, "operator",
                        lambda: {"name": "Alex Example", "email": None, "slug": "alex"})
    data.refresh()
    return store


@pytest.fixture
def client() -> TestClient:
    return TestClient(appmod.app, raise_server_exceptions=False)


def test_clearing_lifts_the_block_in_the_operators_name(client, flagged):
    r = client.post("/api/prs/1/threat/clear-secret", json={"head_sha": "h1"})
    assert r.status_code == 200
    pr = flagged.load_pr(1)
    assert pr.section("threat")["cleared"]["secret-leak"]["by"] == "Alex Example"
    assert not gates.secret_leak_blocks(pr)
    assert not gates.secret_leak_blocks(data.prs()[1])


def test_clearing_a_head_the_operator_did_not_review_is_refused(client, flagged):
    r = client.post("/api/prs/1/threat/clear-secret", json={"head_sha": "h0"})
    assert r.status_code == 409 and "reload" in r.json()["detail"]
    assert gates.secret_leak_blocks(flagged.load_pr(1))
