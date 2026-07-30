"""The agent's `uncluster` CLI — its manual clustering override. Driven end-to-end
as a subprocess (how the sandboxed agent actually invokes it) against a temp store
built from the pipeline's own model, so we never touch the committed store."""
import subprocess
import sys
from pathlib import Path

from pipeline.store import Store

REPO_ROOT = Path(__file__).resolve().parents[3]
UNCLUSTER = REPO_ROOT / "app" / "agent" / "uncluster"


def _seed(root):
    """A store with two PRs jointly in cluster 7; #2 also straddles cluster 8."""
    store = Store(root)
    for n in (1, 2):
        store.save_pr({"pr": n, "meta": {"head_sha": f"sha{n}", "checked_at": "t",
                                         "state": "open", "title": f"p{n}"}})
    store.create_cluster(7, "root seven")
    store.create_cluster(8, "root eight")
    store.edit_cluster(7).set_members([1, 2])
    store.edit_cluster(8).set_members([2])
    return store


def _run(root, args):
    # Minimal env (the sandboxed agent's invocation shape), plus the identity vars
    # settings.py requires and TRIAGE_SKIP_DOTENV so the subprocess stays hermetic
    # against any real .env — mirroring the suite's root conftest.
    return subprocess.run(
        [sys.executable, str(UNCLUSTER), *args, "--store", str(root)],
        env={"PATH": "/usr/bin:/bin", "TRIAGE_SKIP_DOTENV": "1",
             "TRIAGE_REPO": "test-owner/test-repo", "TRIAGE_BOT_LOGIN": "test-bot"},
        capture_output=True, text=True,
    )


def test_detach_from_one_cluster_leaves_others(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    r = _run(root, ["2", "--from", "7"])
    assert r.returncode == 0, r.stderr
    store = Store(root)
    pr2, c7, c8 = store.load_pr(2), store.load_cluster(7), store.load_cluster(8)
    assert pr2 and c7 and c8
    assert pr2.cluster_ids == [8]                    # 7 dropped, 8 kept
    assert c7.prs == [1]                             # cluster 7 keeps its other member
    assert c8.prs == [2]


def test_detach_all_makes_standalone_at_current_head(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    r = _run(root, ["2", "--all"])
    assert r.returncode == 0, r.stderr
    pr = Store(root).load_pr(2)
    assert pr is not None
    # in no cluster, and the empty stamp is current (a confirmed standalone, not
    # an "unreached" PR a later pass would re-cluster).
    assert pr.cluster_ids == []
    cluster_section = pr.section("cluster")
    assert cluster_section is not None
    assert cluster_section["against_head_sha"] == "sha2"


def test_rejects_non_member(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    r = _run(root, ["1", "--from", "8"])            # #1 is not in cluster 8
    assert r.returncode == 2
    assert "not in cluster 8" in r.stderr
    c8 = Store(root).load_cluster(8)
    assert c8 is not None and c8.prs == [2]         # unchanged


def test_rejects_unknown_pr(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    r = _run(root, ["999", "--all"])
    assert r.returncode == 2
    assert "not in the store" in r.stderr


def test_logs_a_run(tmp_path):
    root = tmp_path / "store"
    _seed(root)
    _run(root, ["2", "--from", "7"])
    runs = Store(root).runs()
    last = runs[-1]
    assert last.phase == "cluster:manual-uncluster"
    assert last.raw["stats"] == {"pr": 2, "removed_from": [7]}
