"""merge_pr re-scans the PR at the head it will merge before anything else
runs, and refuses on any threat block the fresh stamp carries (gates.threat_blocks):
a malicious verdict, a committed credential, an unreadable diff, or a head the
scan could not stamp. Dry-run and live alike."""
from types import SimpleNamespace

import pytest

from pipeline import diff_cache, gates, threats
from pipeline.store import Store
from pipeline.testsupport import threat_section
from prospector_app.backend import data, executor, service

REAL_RESCAN = executor._threat_rescan
HEAD = "h" * 40
CLEAN_DIFF = "diff --git a/x.ts b/x.ts\n--- a/x.ts\n+++ b/x.ts\n@@ -1 +1,2 @@\n+const x = 1\n"


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = Store(tmp_path / "store")
    st.save_pr({"pr": 5,
                "meta": {"title": "t", "author": "mallory", "state": "open", "draft": False,
                         "head_sha": HEAD, "checked_at": "2026-10-01T00:00:00+00:00"},
                "threat": threat_section(HEAD, "2026-10-01T00:00:00+00:00")})
    diffs = tmp_path / "diffs"
    diffs.mkdir()
    (diffs / f"{HEAD}.diff").write_text(CLEAN_DIFF)
    monkeypatch.setattr(diff_cache, "DIFFS", diffs)
    monkeypatch.setattr(executor, "_threat_rescan", REAL_RESCAN)
    monkeypatch.setattr(data, "store", lambda: st)
    monkeypatch.setattr(data, "prs", lambda: {5: SimpleNamespace(head_sha=HEAD, linked_issues=[])})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    monkeypatch.setattr(data, "refresh", lambda: None)
    monkeypatch.setattr(service, "live_changed_paths", lambda n: [])
    monkeypatch.setattr(gates, "merge_eligibility", lambda rec, **k: (True, "passed"))
    monkeypatch.setattr(gates, "security_overridable", lambda rec, **k: False)
    monkeypatch.setattr(gates, "verify_overridable", lambda rec, **k: False)
    monkeypatch.setattr(executor, "_pr_live",
                        lambda n: {"state": "open", "merged": False, "head": HEAD,
                                   "mergeable_state": "clean"})
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(executor, "_credit_merge_closed_issues", lambda *a, **k: None)
    return st


def test_a_clean_rescan_lets_the_merge_preview_through(store):
    res = executor.merge_pr(5, dry_run=True)
    assert res["status"] == "dry-run", res


def test_an_author_blocked_since_the_last_scan_refuses_the_merge(store):
    reg = threats.empty_registry()
    threats.block_actor(reg, "mallory", "another PR carried a payload", added="2026-10-02")
    store.save_threats(reg)
    res = executor.merge_pr(5, dry_run=True)
    assert res["status"] == "blocked"
    assert res["detail"].startswith("threat rescan: malicious")
    assert store.load_pr(5).threat_verdict == "malicious"


def test_a_head_the_rescan_cannot_read_refuses_the_merge(store, monkeypatch):
    (diff_cache.DIFFS / f"{HEAD}.diff").unlink()
    monkeypatch.setattr(diff_cache, "fetch_diffs", lambda manifest, **k: (0, len(manifest)))
    rec = store.load_pr(5)
    rec.raw["threat"]["against_head_sha"] = "older"
    store.save_pr(rec.raw)
    res = executor.merge_pr(5, dry_run=True)
    assert res["status"] == "blocked"
    assert res["detail"] == "threat rescan: threat scan stale or missing"
