"""merge_pr's compile preflight: a live merge runs it after the dry-run branch
and before any override write, blocks fail-closed on anything but a clean
pass, records the run to the activity log, and never runs it on dry-run or on
a deployment with no compile_cmd configured."""
from types import SimpleNamespace

from prospector_app.backend import caps
from prospector_app.backend import data
from prospector_app.backend import executor
from pipeline import compile_preflight
from pipeline import gates

BASE = "f" * 40


class _EditPr:
    def __init__(self, events: list):
        self._events = events

    def log_security_override(self, reason: str, *, by: str | None = None) -> None:
        self._events.append(("override", reason, by))


class _Store:
    def __init__(self, events: list):
        self._events = events

    def edit_pr(self, n: int) -> _EditPr:
        return _EditPr(self._events)


def _setup(monkeypatch, events: list, recorded: list, *, verdict="GREEN",
           overridable=False):
    rec = SimpleNamespace(head_sha="h" * 40, security_verdict=verdict,
                          linked_issues=[])
    monkeypatch.setattr(data, "prs", lambda: {5: rec})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    monkeypatch.setattr(data, "store", lambda: _Store(events))
    monkeypatch.setattr(data, "refresh", lambda: None)
    monkeypatch.setattr(executor, "_changed_paths", lambda n: [])
    monkeypatch.setattr(
        gates, "merge_eligibility",
        lambda rec, today=None, changed_paths=None, override_reason=None:
            (True, "passed") if verdict == "GREEN" or override_reason
            else (False, f"security {verdict}"))
    monkeypatch.setattr(gates, "security_overridable",
                        lambda rec, today=None, changed_paths=None: overridable)
    monkeypatch.setattr(gates, "verify_overridable",
                        lambda rec, today=None, changed_paths=None: False)
    monkeypatch.setattr(executor, "_pr_live",
                        lambda n: {"state": "open", "merged": False, "head": "h" * 40,
                                   "mergeable_state": "clean"})
    monkeypatch.setattr(executor.activity, "record",
                        lambda kind, **f: recorded.append({"kind": kind, **f}))
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "tok")
    monkeypatch.setattr(caps, "capabilities", lambda: {"login": "operator"})

    def fake_merge(cmd, token):
        events.append(("merge", cmd))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(executor, "bot_merge_run", fake_merge)
    monkeypatch.setattr(executor, "_reflect_state", lambda n, **k: None)
    monkeypatch.setattr(executor, "_credit_merge_closed_issues",
                        lambda rec, n, dry_run: None)


def test_live_merge_runs_preflight_and_proceeds_on_pass(monkeypatch):
    events: list = []
    recorded: list = []
    _setup(monkeypatch, events, recorded)
    monkeypatch.setattr(compile_preflight, "run_for_merge",
                        lambda n, head: {"cmd": "pnpm -r typecheck", "pr": n,
                                         "head_sha": head, "base_sha": BASE,
                                         "exit": 0, "duration_s": 1.0})
    res = executor.merge_pr(5, dry_run=False)
    assert res["status"] == "merged"
    assert res["compile_preflight"]["ok"] is True
    assert [e[0] for e in events] == ["merge"]
    merge_events = [r for r in recorded if r.get("status") == "merged"]
    assert merge_events and merge_events[0]["compile_preflight"]["exit"] == 0


def test_live_merge_blocks_on_compile_failure(monkeypatch):
    events: list = []
    recorded: list = []
    _setup(monkeypatch, events, recorded)
    monkeypatch.setattr(compile_preflight, "run_for_merge",
                        lambda n, head: {"cmd": "pnpm -r typecheck", "pr": n,
                                         "head_sha": head, "base_sha": BASE,
                                         "exit": 20,
                                         "error_excerpt": "error TS2739: missing",
                                         "duration_s": 1.0})
    res = executor.merge_pr(5, dry_run=False)
    assert res["status"] == "blocked"
    assert res["detail"].startswith("compile preflight: ")
    assert "TS2739" in res["detail"]
    assert events == []  # no merge fired
    blocked = [r for r in recorded if r.get("status") == "blocked"]
    assert blocked and blocked[0]["kind"] == "merge"
    assert blocked[0]["dry_run"] is False
    assert blocked[0]["compile_preflight"]["ok"] is False


def test_sandbox_unavailable_blocks_fail_closed(monkeypatch):
    events: list = []
    recorded: list = []
    _setup(monkeypatch, events, recorded)
    monkeypatch.setattr(compile_preflight, "run_for_merge",
                        lambda n, head: {"cmd": "pnpm -r typecheck", "pr": n,
                                         "head_sha": head,
                                         "error": "OSError: docker not found",
                                         "duration_s": 0.1})
    res = executor.merge_pr(5, dry_run=False)
    assert res["status"] == "blocked"
    assert "docker not found" in res["detail"]
    assert events == []


def test_dry_run_never_runs_the_preflight(monkeypatch):
    events: list = []
    recorded: list = []
    _setup(monkeypatch, events, recorded)
    monkeypatch.setattr(compile_preflight, "run_for_merge",
                        lambda n, head: (_ for _ in ()).throw(
                            AssertionError("preflight ran on dry-run")))
    res = executor.merge_pr(5, dry_run=True)
    assert res["status"] == "dry-run"
    assert "compile_preflight" not in res


def test_unconfigured_deployment_merges_without_preflight(monkeypatch):
    events: list = []
    recorded: list = []
    _setup(monkeypatch, events, recorded)
    monkeypatch.setattr(compile_preflight, "run_for_merge", lambda n, head: None)
    res = executor.merge_pr(5, dry_run=False)
    assert res["status"] == "merged"
    assert "compile_preflight" not in res


def test_preflight_block_precedes_override_logging(monkeypatch):
    events: list = []
    recorded: list = []
    _setup(monkeypatch, events, recorded, verdict="YELLOW", overridable=True)
    monkeypatch.setattr(compile_preflight, "run_for_merge",
                        lambda n, head: {"cmd": "c", "pr": n, "head_sha": head,
                                         "base_sha": BASE, "exit": 20,
                                         "duration_s": 1.0})
    res = executor.merge_pr(5, dry_run=False, reason="ship it")
    assert res["status"] == "blocked"
    assert events == []  # neither the override write nor the merge happened


def test_preflight_pass_then_override_logs_then_merges(monkeypatch):
    events: list = []
    recorded: list = []
    _setup(monkeypatch, events, recorded, verdict="YELLOW", overridable=True)
    monkeypatch.setattr(compile_preflight, "run_for_merge",
                        lambda n, head: {"cmd": "c", "pr": n, "head_sha": head,
                                         "base_sha": BASE, "exit": 0,
                                         "duration_s": 1.0})
    res = executor.merge_pr(5, dry_run=False, reason="mirrors upstream fix")
    assert res["status"] == "merged"
    assert [e[0] for e in events] == ["override", "merge"]
