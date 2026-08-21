"""The JSON→DB importer is lossless: import a file store, dump it back, and the
records match."""
import json

from pipeline import store_migrate
from pipeline.store import Store


def _write_json_store(root):
    (root / "prs").mkdir(parents=True)
    (root / "clusters").mkdir(parents=True)
    pr = {"pr": 5, "meta": {"title": "t", "state": "open", "head_sha": "h",
                            "updated_at": "u", "checked_at": "c"}}
    (root / "prs" / "5.json").write_text(json.dumps(pr, indent=1, sort_keys=True) + "\n")
    cl = {"id": 1, "root_problem": "rp", "prs": [5], "outcome": None, "checked_at": "c"}
    (root / "clusters" / "001.json").write_text(json.dumps(cl, indent=1, sort_keys=True) + "\n")
    (root / "runs.jsonl").write_text(json.dumps({"phase": "ingest", "ts": "c"}) + "\n")
    (root / "threats.json").write_text(json.dumps({"actors": {"bad": 1}, "incidents": []}))
    (root / "action_items.json").write_text(json.dumps({"items": [{"id": "x"}]}))
    return pr, cl


def test_import_then_read_via_store(tmp_path):
    src = tmp_path / "json"
    pr, cl = _write_json_store(src)
    db_root = tmp_path / "db"
    store_migrate.import_pr_store(src, Store(db_root))
    s = Store(db_root)
    assert s.load_pr(5).raw == pr
    assert s.load_cluster(1).raw == cl
    assert [r.raw for r in s.runs()] == [{"phase": "ingest", "ts": "c"}]
    assert s.load_threats() == {"actors": {"bad": 1}, "incidents": []}
    assert s.load_action_items() == {"items": [{"id": "x"}]}


def test_roundtrip_dump_matches_source(tmp_path):
    src = tmp_path / "json"
    _write_json_store(src)
    db_root = tmp_path / "db"
    store_migrate.import_pr_store(src, Store(db_root))
    out = tmp_path / "out"
    store_migrate.dump_pr_store(Store(db_root), out)
    for rel in ("prs/5.json", "clusters/001.json", "threats.json", "action_items.json"):
        assert json.loads((out / rel).read_text()) == json.loads((src / rel).read_text())
    assert [json.loads(line) for line in (out / "runs.jsonl").read_text().splitlines()] == \
           [json.loads(line) for line in (src / "runs.jsonl").read_text().splitlines()]


def test_import_into_configured_store(tmp_path, monkeypatch):
    src = tmp_path / "json"
    pr, _cl = _write_json_store(src)
    cfg_db = tmp_path / "configured" / "store.db"
    monkeypatch.setenv("TRIAGE_STORE_URL", f"sqlite:///{cfg_db}")
    store_migrate.import_pr_store(src, store_migrate.dest_store("@env"))
    from pipeline.store import Store
    assert Store().load_pr(5).raw == pr
