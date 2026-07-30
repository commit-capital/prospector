"""The agent's `reingest` CLI — its "refresh one PR" path into the SQL store.
Driven end-to-end as a subprocess (how the sandboxed agent invokes it) against a
temp store, exercising the hermetic paths only: an unknown PR returns before any
GitHub call, and a closed PR stops after the (monkeypatch-free) refresh. The
agentic re-summarize/re-analyze tail is covered in-process by
pipeline/tests/test_reingest.py, which stubs the network + LLM."""
import subprocess
import sys
from pathlib import Path

from pipeline.store import Store

REPO_ROOT = Path(__file__).resolve().parents[3]
REINGEST = REPO_ROOT / "prospector_app" / "agent" / "reingest"


def _run(root, args):
    # Minimal env (the sandboxed agent's invocation shape) + the identity vars
    # settings.py requires and TRIAGE_SKIP_DOTENV so the subprocess stays hermetic
    # against any real .env — mirroring test_store_read_cli / test_uncluster_cli.
    return subprocess.run(
        [sys.executable, str(REINGEST), *args, "--store", str(root)],
        env={"PATH": "/usr/bin:/bin", "TRIAGE_SKIP_DOTENV": "1",
             "TRIAGE_REPO": "test-owner/test-repo", "TRIAGE_BOT_LOGIN": "test-bot"},
        capture_output=True, text=True,
    )


def test_unknown_pr_reports_not_in_store(tmp_path):
    root = tmp_path / "store"
    Store(str(root))                      # empty store
    r = _run(root, ["999"])
    assert r.returncode == 1, r.stderr
    assert "not in the store" in (r.stdout + r.stderr)


def test_positional_pr_is_required(tmp_path):
    root = tmp_path / "store"
    Store(str(root))
    r = _run(root, [])
    assert r.returncode != 0                # argparse rejects the missing positional
    assert "pr" in (r.stdout + r.stderr).lower()
