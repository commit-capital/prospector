"""The command wrapper around the lane core: base resolution, the report source,
the result file, the ledger row, and the exit code. `fix_lane.run`, the stores,
and the base primitives are mocked; what is pinned here is the CLI's own wiring."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from issue_triage import fix_lane
from pipeline import prove, storekit


def _result(ending: str = "fixed", *, fault: bool = False,
            detail: str = "reproduced and fixed") -> fix_lane.LaneResult:
    return fix_lane.LaneResult(
        ending=ending, fault=fault, detail=detail,
        reproduction={"outcome": "reproduced"}, result={"summary": "guarded the input"},
        agent_runs=5)


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(tmp_path / "vscratch"))
    monkeypatch.setenv("TRIAGE_WORKER_ID", "test-host")
    base = prove.PinnedBase(sha="a" * 40, tier=0,
                            image="pr-verify-base:aaaaaaaaaaaa-t0", clone=tmp_path / "clone")
    state: dict = {"appended": [], "specs": [], "stored": None,
                   "fetched": {"title": "Crash on empty", "body": "boom", "state": "open"},
                   "result": _result()}

    class FakeStore:
        def __init__(self, root=None):
            pass

        def load_issue(self, n):
            return state["stored"]

        def append_run(self, record):
            storekit.parse_run(record)
            state["appended"].append(record)

    def fake_run(spec, *, workdir, on_step, still_valid):
        state["specs"].append(spec)
        return state["result"]

    monkeypatch.setattr(fix_lane, "IssueStore", FakeStore)
    monkeypatch.setattr(fix_lane, "Store", lambda: None)
    monkeypatch.setattr(fix_lane, "run", fake_run)
    monkeypatch.setattr(prove, "pinned", lambda store: base)
    monkeypatch.setattr(prove, "held", lambda sha, tier: base)
    monkeypatch.setattr(fix_lane.fetch_issues, "fetch_issue", lambda n: state["fetched"])
    return SimpleNamespace(base=base, state=state, scratch=tmp_path / "vscratch")


def _result_file(cli, n: int = 7) -> dict:
    return json.loads((cli.scratch / "issue-fix" / f"issue-{n}" / "result.json").read_text())


def test_a_verdict_ending_writes_the_result_file_and_exits_zero(cli):
    code = fix_lane.main(["--issue", "7"])
    assert code == 0
    payload = _result_file(cli)
    assert set(payload) == {"issue", "report_sha", "base_sha", "action", "ending",
                            "fault", "detail", "agent_runs", "started", "finished",
                            "reproduction", "result"}
    assert payload["issue"] == 7
    assert payload["action"] == "fix"
    assert payload["ending"] == "fixed"
    assert payload["fault"] is False
    assert payload["base_sha"] == "a" * 40
    assert payload["report_sha"] == fix_lane.report_sha("Crash on empty", "boom")
    assert payload["reproduction"] == {"outcome": "reproduced"}
    assert payload["result"] == {"summary": "guarded the input"}
    assert payload["started"] and payload["finished"]


def test_the_ledger_row_parses_and_carries_the_stats(cli):
    fix_lane.main(["--issue", "7"])
    assert len(cli.state["appended"]) == 1
    record = cli.state["appended"][0]
    run = storekit.parse_run(record)
    assert isinstance(run, storekit.PhaseRun)
    assert run.phase == "issue-fix:run"
    assert record["issue"] == 7 and record["trigger"] == "cli"
    stats = record["stats"]
    assert stats["action"] == "fix"
    assert stats["ending"] == "fixed"
    assert stats["fault"] is False
    assert stats["detail"] == "reproduced and fixed"
    assert stats["host"] == "test-host"
    assert stats["base_sha"] == "a" * 40
    assert stats["report_sha"] == fix_lane.report_sha("Crash on empty", "boom")
    assert stats["agent_runs"] == 5


def test_reproduce_only_sets_action_reproduce(cli):
    cli.state["result"] = fix_lane.LaneResult(
        ending="reproduced", fault=False, detail="reproduced on the pinned base",
        reproduction={"outcome": "reproduced"}, agent_runs=2)
    code = fix_lane.main(["--issue", "7", "--reproduce-only"])
    assert code == 0
    assert cli.state["specs"][0].action == "reproduce"
    assert _result_file(cli)["action"] == "reproduce"
    assert cli.state["appended"][0]["stats"]["action"] == "reproduce"


def test_a_fault_ending_exits_one_but_still_records(cli):
    cli.state["result"] = _result("sandbox", fault=True, detail="the sandbox could not run")
    code = fix_lane.main(["--issue", "7"])
    assert code == 1
    assert _result_file(cli)["fault"] is True
    stats = cli.state["appended"][0]["stats"]
    assert stats["ending"] == "sandbox" and stats["fault"] is True


def test_no_base_exits_two_prints_the_reason_and_writes_no_ledger_row(cli, monkeypatch, capsys):
    def unpinned(store):
        raise prove.NoBase("the base image pr-verify-base:abc-t0 is not on this machine")

    monkeypatch.setattr(prove, "pinned", unpinned)
    code = fix_lane.main(["--issue", "7"])
    assert code == 2
    out = capsys.readouterr()
    assert "not on this machine" in (out.out + out.err)
    assert cli.state["appended"] == []
    assert not (cli.scratch / "issue-fix" / "issue-7" / "result.json").exists()


def test_an_unknown_issue_exits_two_with_no_ledger_row(cli, monkeypatch):
    cli.state["stored"] = None
    monkeypatch.setattr(fix_lane.fetch_issues, "fetch_issue", lambda n: None)
    code = fix_lane.main(["--issue", "999"])
    assert code == 2
    assert cli.state["appended"] == []


def test_a_base_sha_proves_against_the_named_held_base(cli, monkeypatch):
    seen: dict = {}

    def held(sha, tier):
        seen["sha"], seen["tier"] = sha, tier
        return cli.base

    monkeypatch.setattr(prove, "held", held)
    code = fix_lane.main(["--issue", "7", "--base-sha", "d" * 40, "--tier", "1"])
    assert code == 0
    assert seen == {"sha": "d" * 40, "tier": 1}


def test_the_stored_issue_is_preferred_over_a_live_fetch(cli):
    cli.state["stored"] = SimpleNamespace(title="Stored title", body="stored body")
    fix_lane.main(["--issue", "7"])
    spec = cli.state["specs"][0]
    assert (spec.title, spec.body) == ("Stored title", "stored body")
    assert _result_file(cli)["report_sha"] == fix_lane.report_sha("Stored title", "stored body")
