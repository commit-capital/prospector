"""GET /api/prs/{n}/evidence lists a flagged PR's captures without diff bytes;
the bundle route serves one capture as a hash-checked zip download and records
the download in the runs ledger."""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from pipeline import threat_evidence as te
from pipeline.store import Store
from pipeline.tests.test_threat_evidence import FLAG, FakeGitHub
from prospector_app.backend import activity
from prospector_app.backend import app as appmod
from prospector_app.backend import data


@pytest.fixture
def seeded(tmp_path, monkeypatch) -> Store:
    store = Store(tmp_path)
    te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path)
    monkeypatch.setattr(data, "store", lambda: store)
    monkeypatch.setattr(activity, "operator",
                        lambda: {"name": "Alex Example", "email": None, "slug": "alex"})
    return store


@pytest.fixture
def client() -> TestClient:
    return TestClient(appmod.app, raise_server_exceptions=False)


def test_evidence_list_is_metadata_only(client, seeded):
    r = client.get("/api/prs/11987/evidence")
    assert r.status_code == 200
    [item] = r.json()["items"]
    assert item["complete"] is True and item["head_sha"] == FLAG.head_sha
    assert "diff_gz" not in item and "fromCharCode" not in r.text


def test_evidence_list_for_an_unflagged_pr_is_empty(client, seeded):
    r = client.get("/api/prs/1/evidence")
    assert r.status_code == 200 and r.json() == {"items": []}


def test_bundle_download_headers_and_ledger(client, seeded):
    cid = client.get("/api/prs/11987/evidence").json()["items"][0]["id"]
    r = client.get(f"/api/prs/11987/evidence/{cid}/bundle.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"].startswith("attachment;")
    assert r.headers["x-content-type-options"] == "nosniff"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert any(n.endswith("/SHA256SUMS") for n in names)
    [run] = [x for x in seeded.runs() if x.phase == "threat-evidence:export"]
    assert run.raw["via"] == "app" and run.raw["operator"] == "Alex Example"


def test_bundle_for_another_prs_capture_is_404(client, seeded):
    cid = client.get("/api/prs/11987/evidence").json()["items"][0]["id"]
    assert client.get(f"/api/prs/1/evidence/{cid}/bundle.zip").status_code == 404
    assert client.get("/api/prs/11987/evidence/999/bundle.zip").status_code == 404


def test_bundle_with_a_hash_mismatch_is_409_and_not_logged(client, seeded):
    from pipeline import schema
    cid = client.get("/api/prs/11987/evidence").json()["items"][0]["id"]
    import gzip
    with seeded.engine.begin() as conn:
        conn.execute(schema.threat_evidence.update()
                     .where(schema.threat_evidence.c.id == cid)
                     .values(diff_gz=gzip.compress(b"tampered")))
    r = client.get(f"/api/prs/11987/evidence/{cid}/bundle.zip")
    assert r.status_code == 409
    assert not [x for x in seeded.runs() if x.phase == "threat-evidence:export"]
