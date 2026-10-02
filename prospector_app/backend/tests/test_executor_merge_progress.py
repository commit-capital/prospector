"""merge_pr's record of a click: every refusal lands in the activity log, a live
merge records `started` before its compile preflight and names each step it
reaches in merge_progress, and a second live merge of the same PR is refused
while the first runs."""
from types import SimpleNamespace

from prospector_app.backend import caps
from prospector_app.backend import data
from prospector_app.backend import executor
from prospector_app.backend import merge_progress
from prospector_app.backend import service
from pipeline import compile_preflight
from pipeline import gates
from pipeline import progress

HEAD = "a" * 40


def _setup(monkeypatch, recorded: list, *, gate_ok: bool = True, live: dict | None = None):
    rec = SimpleNamespace(head_sha=HEAD, security_verdict="GREEN", linked_issues=[])
    monkeypatch.setattr(data, "prs", lambda: {5: rec})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    monkeypatch.setattr(data, "refresh", lambda: None)
    monkeypatch.setattr(service, "live_changed_paths", lambda n: [])
    monkeypatch.setattr(
        gates, "merge_eligibility",
        lambda rec, today=None, changed_paths=None, override_reason=None:
            (True, "passed") if gate_ok else (False, "CI failing"))
    monkeypatch.setattr(gates, "security_overridable",
                        lambda rec, today=None, changed_paths=None: False)
    monkeypatch.setattr(gates, "verify_overridable",
                        lambda rec, today=None, changed_paths=None: False)
    monkeypatch.setattr(executor, "_pr_live", lambda n: live if live is not None else {
        "state": "open", "merged": False, "head": HEAD, "mergeable_state": "clean"})
    monkeypatch.setattr(executor.activity, "record",
                        lambda kind, **f: recorded.append({"kind": kind, **f}))
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "tok")
    monkeypatch.setattr(caps, "capabilities", lambda: {"login": "operator"})
    monkeypatch.setattr(executor, "bot_merge_run",
                        lambda cmd, token: SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setattr(executor, "_reflect_state", lambda n, **k: None)
    monkeypatch.setattr(executor, "_credit_merge_closed_issues",
                        lambda rec, n, dry_run: None)


def test_a_gate_refusal_is_logged(monkeypatch):
    recorded: list = []
    _setup(monkeypatch, recorded, gate_ok=False)
    res = executor.merge_pr(5, dry_run=False)
    assert res["status"] == "blocked"
    assert [(r["kind"], r["status"], r["detail"]) for r in recorded] == [
        ("merge", "blocked", "merge gate: CI failing")]
    assert merge_progress.get(5) is None


def test_a_preflight_refusal_is_logged(monkeypatch):
    recorded: list = []
    _setup(monkeypatch, recorded, live={"state": "closed", "merged": False, "head": HEAD})
    res = executor.merge_pr(5, dry_run=True)
    assert res["status"] == "blocked"
    assert len(recorded) == 1
    assert recorded[0]["status"] == "blocked"
    assert recorded[0]["detail"].startswith("pre-flight: ")
    assert recorded[0]["dry_run"] is True


def test_a_live_merge_records_its_start_and_reports_each_step(monkeypatch):
    recorded: list = []
    _setup(monkeypatch, recorded)
    seen: list = []

    def run_for_merge(n, head):
        statuses = [r["status"] for r in recorded]
        assert statuses == ["started"]
        progress.step("building img")
        seen.append(merge_progress.get(5))
        return {"cmd": "tsc", "pr": n, "head_sha": head, "exit": 0, "duration_s": 1.0}

    monkeypatch.setattr(compile_preflight, "run_for_merge", run_for_merge)
    res = executor.merge_pr(5, dry_run=False)
    assert res["status"] == "merged"
    assert [r["status"] for r in recorded] == ["started", "merged"]
    assert recorded[0]["head_sha"] == HEAD
    assert seen[0] is not None and seen[0]["step"] == "building img"
    assert seen[0]["head_sha"] == HEAD
    assert merge_progress.get(5) is None


def test_a_second_live_merge_of_the_same_pr_is_refused(monkeypatch):
    recorded: list = []
    _setup(monkeypatch, recorded)
    inner: list = []

    def run_for_merge(n, head):
        inner.append(executor.merge_pr(5, dry_run=False))
        return None

    monkeypatch.setattr(compile_preflight, "run_for_merge", run_for_merge)
    res = executor.merge_pr(5, dry_run=False)
    assert res["status"] == "merged"
    assert inner[0]["status"] == "blocked"
    assert "already running" in inner[0]["detail"]
    assert merge_progress.get(5) is None


def test_progress_clears_when_the_merge_raises(monkeypatch):
    recorded: list = []
    _setup(monkeypatch, recorded)

    def run_for_merge(n, head):
        raise RuntimeError("boom")

    monkeypatch.setattr(compile_preflight, "run_for_merge", run_for_merge)
    try:
        executor.merge_pr(5, dry_run=False)
    except RuntimeError:
        pass
    assert merge_progress.get(5) is None
