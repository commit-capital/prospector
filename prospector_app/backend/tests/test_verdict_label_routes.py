"""GET /api/verdicts lists a subject's recorded reviewer runs; POST
/api/verdicts/{id}/label sets one's label in the operator's name."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pipeline import verdict_log
from pipeline.store import Store
from prospector_app.backend import activity
from prospector_app.backend import app as appmod
from prospector_app.backend import data


@pytest.fixture
def store(tmp_path, monkeypatch) -> Store:
    st = Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setattr(activity, "operator",
                        lambda: {"name": "Alex Example", "email": None, "slug": "alex"})
    return st


@pytest.fixture
def client() -> TestClient:
    return TestClient(appmod.app, raise_server_exceptions=False)


def _row(store: Store, kind: str = "pr", n: int = 7, verdict: str = "pass") -> int:
    vid = verdict_log.record("design", verdict_log.Subject(kind, n), verdict=verdict,  # type: ignore[arg-type]
                             prompt_sha="p" * 16, model="opus", data={"findings": []},
                             inputs={"title": "t"}, store=store)
    assert vid is not None
    return vid


def test_lists_a_subjects_verdicts(client, store):
    first, second = _row(store), _row(store, verdict="fail")
    _row(store, "issue", 7)
    r = client.get("/api/verdicts", params={"subject_kind": "pr", "subject": 7})
    assert r.status_code == 200
    rows = r.json()["verdicts"]
    assert {row["id"] for row in rows} == {first, second}
    assert all("inputs_gz" not in row for row in rows)


def test_list_refuses_an_unknown_kind(client, store):
    assert client.get("/api/verdicts", params={"subject_kind": "cluster",
                                                "subject": 7}).status_code == 422


def test_label_route_stores_operator(client, store):
    vid = _row(store)
    r = client.post(f"/api/verdicts/{vid}/label", json={"expected": "fail", "note": "dup"})
    assert r.status_code == 200 and r.json()["label"]["by"] == "Alex Example"
    rec = store.review_verdict(vid)
    assert rec is not None and rec.label is not None and rec.label["expected"] == "fail"


def test_label_route_refuses_failed_row_409(client, store):
    r = client.post(f"/api/verdicts/{_row(store, verdict='failed')}/label",
                    json={"expected": "pass"})
    assert r.status_code == 409 and "never judged" in r.json()["detail"]


def test_label_route_missing_404(client, store):
    assert client.post("/api/verdicts/999/label", json={"expected": "pass"}).status_code == 404


def test_label_route_refuses_malformed_findings_422(client, store):
    r = client.post(f"/api/verdicts/{_row(store)}/label",
                    json={"expected": "pass", "findings": ["real"]})
    assert r.status_code == 422
