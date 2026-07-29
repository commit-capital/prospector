"""status_md over a mixed runs ledger: phase runs summarized, store-edits listed."""
from __future__ import annotations

from pipeline import views
from pipeline.store import Store


def test_status_md_renders_mixed_ledger(tmp_path):
    store = Store(tmp_path)
    store.append_run({"phase": "ingest", "started": "t0", "finished": "t1",
                      "stats": {"prs": 2}})
    store.append_run({"action": "store-edit", "transform": "fix_things",
                      "table": "prs", "examined": 10, "changed": [1, 2],
                      "deleted": [3], "backup": "/tmp/b.json", "ts": "t2"})
    out = views.status_md(store)
    assert "**ingest**: finished t1" in out
    assert "## Store edits" in out
    assert "**fix_things** on `prs` at t2: 2 changed, 1 deleted of 10 examined" in out


def test_status_md_empty_store(tmp_path):
    out = views.status_md(Store(tmp_path))
    assert "## Last runs" not in out
    assert "## Store edits" not in out
