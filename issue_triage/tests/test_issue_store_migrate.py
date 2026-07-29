"""The JSON→DB importer is lossless: import a file store, dump it back, and the
records match."""
from __future__ import annotations

import json

from issue_triage import issue_store_migrate
from issue_triage.issue_store import IssueStore


def _write_json_store(root):
    (root / "issues").mkdir(parents=True)
    (root / "issue_clusters").mkdir(parents=True)
    issue = {"issue": 5, "meta": {"title": "t", "state": "open", "updated_at": "u"}}
    (root / "issues" / "5.json").write_text(
        json.dumps(issue, indent=1, sort_keys=True) + "\n")
    cl = {"id": 1, "members": [5], "title": "tc"}
    (root / "issue_clusters" / "001.json").write_text(
        json.dumps(cl, indent=1, sort_keys=True) + "\n")
    (root / "runs.jsonl").write_text(json.dumps({"phase": "ingest", "ts": "c"}) + "\n")
    return issue, cl


def test_import_then_read_via_store(tmp_path):
    src = tmp_path / "json"
    issue, cl = _write_json_store(src)
    db_root = tmp_path / "db"
    issue_store_migrate.import_issue_store(src, IssueStore(db_root))
    s = IssueStore(db_root)
    assert s.load_issue(5).raw == issue
    assert s.load_issue_cluster(1).raw == cl
    assert [r.raw for r in s.runs()] == [{"phase": "ingest", "ts": "c"}]


def test_roundtrip_dump_matches_source(tmp_path):
    src = tmp_path / "json"
    _write_json_store(src)
    db_root = tmp_path / "db"
    issue_store_migrate.import_issue_store(src, IssueStore(db_root))
    out = tmp_path / "out"
    issue_store_migrate.dump_issue_store(IssueStore(db_root), out)
    for rel in ("issues/5.json", "issue_clusters/001.json"):
        assert json.loads((out / rel).read_text()) == json.loads((src / rel).read_text())
    assert [json.loads(line) for line in (out / "runs.jsonl").read_text().splitlines()] == \
           [json.loads(line) for line in (src / "runs.jsonl").read_text().splitlines()]


def test_import_into_configured_store(tmp_path, monkeypatch):
    src = tmp_path / "json"
    issue, _cl = _write_json_store(src)
    cfg_db = tmp_path / "configured" / "store.db"
    monkeypatch.setattr("pipeline.settings.STORE_URL", f"sqlite:///{cfg_db}")
    issue_store_migrate.import_issue_store(src, issue_store_migrate.dest_store("@env"))
    from issue_triage.issue_store import IssueStore
    assert IssueStore().load_issue(5).raw == issue
