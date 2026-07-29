"""The agent's `store-read` CLI — its read window into the SQL store. Driven
end-to-end as a subprocess (how the sandboxed agent actually invokes it) against
a temp store built from the pipeline's own model, so we never touch the committed
store."""
import json
import subprocess
import sys
from pathlib import Path

from issue_triage.issue_store import IssueStore
from pipeline.store import Store

REPO_ROOT = Path(__file__).resolve().parents[3]
STORE_READ = REPO_ROOT / "review_cockpit" / "agent" / "store-read"


def _seed(root):
    """A store with one analyzed PR in cluster 7, one analyzed issue, plus a
    threat registry entry."""
    store = Store(root)
    store.save_pr({
        "pr": 1,
        "meta": {"head_sha": "sha1", "checked_at": "t", "state": "open", "title": "p1"},
        "analysis": {"against_head_sha": "sha1", "checked_at": "t",
                     "disposition": "close-dup", "canonical": 2,
                     "rationale": "dup of 2"},
    })
    store.create_cluster(7, "root seven")
    store.edit_cluster(7).set_members([1])
    store.save_threats({"actors": {"baddie": {"added": "2026-01-01", "incidents": [9]}},
                        "incidents": []})
    IssueStore(root).save_issue({
        "issue": 42,
        "meta": {"title": "it crashes", "state": "open", "updated_at": "t"},
        "analysis": {"disposition": "needs-human", "rationale": "unclear repro"},
    })
    return store


def _run(root, args):
    # Minimal env (the sandboxed agent's invocation shape), plus the identity vars
    # settings.py requires and TRIAGE_SKIP_DOTENV so the subprocess stays hermetic
    # against any real .env — mirroring test_uncluster_cli.
    return subprocess.run(
        [sys.executable, str(STORE_READ), *args, "--store", str(root)],
        env={"PATH": "/usr/bin:/bin", "TRIAGE_SKIP_DOTENV": "1",
             "TRIAGE_REPO": "test-owner/test-repo", "TRIAGE_BOT_LOGIN": "test-bot"},
        capture_output=True, text=True,
    )


def test_reads_whole_pr_record(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    r = _run(root, ["pr", "1"])
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    assert rec["pr"] == 1
    assert rec["analysis"]["disposition"] == "close-dup"


def test_reads_one_section(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    r = _run(root, ["pr", "1", "--section", "analysis"])
    assert r.returncode == 0, r.stderr
    section = json.loads(r.stdout)
    assert section["canonical"] == 2
    assert section["rationale"] == "dup of 2"


def test_missing_section_prints_null_and_notes(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    r = _run(root, ["pr", "1", "--section", "security"])
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) is None          # `null` on stdout
    assert "no `security` section" in r.stderr    # explained, not silent


def test_unknown_pr_prints_null_not_error(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    r = _run(root, ["pr", "999"])
    assert r.returncode == 0
    assert json.loads(r.stdout) is None
    assert "not in the store" in r.stderr


def test_reads_cluster_record(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    r = _run(root, ["cluster", "7"])
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    assert rec["id"] == 7
    assert rec["prs"] == [1]
    assert rec["root_problem"] == "root seven"


def test_reads_whole_issue_record(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    r = _run(root, ["issue", "42"])
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    assert rec["issue"] == 42
    assert rec["analysis"]["disposition"] == "needs-human"


def test_reads_one_issue_section(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    r = _run(root, ["issue", "42", "--section", "analysis"])
    assert r.returncode == 0, r.stderr
    section = json.loads(r.stdout)
    assert section["rationale"] == "unclear repro"


def test_missing_issue_section_prints_null_and_notes(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    r = _run(root, ["issue", "42", "--section", "repro"])
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) is None
    assert "no `repro` section" in r.stderr


def test_unknown_issue_prints_null_not_error(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    r = _run(root, ["issue", "999"])
    assert r.returncode == 0
    assert json.loads(r.stdout) is None
    assert "not in the store" in r.stderr


def test_reads_threat_registry(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    r = _run(root, ["threats"])
    assert r.returncode == 0, r.stderr
    reg = json.loads(r.stdout)
    assert "baddie" in reg["actors"]


def _seed_activity(root):
    """Landed + dry-run activity events in the same store DB the CLI reads."""
    from pipeline import schema
    from pipeline import storekit
    eng = storekit.get_engine(storekit.resolve_url(root))
    schema.METADATA.create_all(eng)
    rows = [
        {"at": "2026-07-17T10:00:00", "kind": "close", "action": "CLOSE_STALE",
         "status": "executed", "reason": "stale", "identity": "test-bot",
         "operator": "Casey", "pr": 1, "dry_run": False},
        {"at": "2026-07-17T11:00:00", "kind": "issue-close", "action": "CLOSE_ISSUE",
         "status": "closed", "reason": "not planned", "identity": "test-bot",
         "operator": "Casey", "issue": 42, "dry_run": False},
        {"at": "2026-07-17T12:00:00", "kind": "close", "status": "dry-run",
         "identity": "test-bot", "operator": "Casey", "pr": 1, "dry_run": True},
    ]
    with eng.begin() as conn:
        for r in rows:
            conn.execute(schema.activity.insert().values(**schema.activity_row(r)))


def test_activity_pr_lists_landed_actions_only(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    _seed_activity(root)
    r = _run(root, ["activity", "pr", "1"])
    assert r.returncode == 0, r.stderr
    items = json.loads(r.stdout)
    assert [i["kind"] for i in items] == ["close"]   # the dry-run preview is excluded
    assert items[0]["reason"] == "stale"
    assert items[0]["operator"] == "Casey"


def test_activity_issue_lists_landed_actions(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    _seed_activity(root)
    r = _run(root, ["activity", "issue", "42"])
    assert r.returncode == 0, r.stderr
    items = json.loads(r.stdout)
    assert [i["kind"] for i in items] == ["issue-close"]
    assert items[0]["reason"] == "not planned"


def test_activity_empty_prints_list_and_notes(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    _seed_activity(root)
    r = _run(root, ["activity", "pr", "999"])
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == []
    assert "no executed actions" in r.stderr


def test_activity_recent_includes_dry_runs(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    _seed_activity(root)
    r = _run(root, ["activity", "recent", "--limit", "2"])
    assert r.returncode == 0, r.stderr
    items = json.loads(r.stdout)
    assert len(items) == 2
    assert items[0]["dry_run"] is True   # newest first; the preview is visible here
