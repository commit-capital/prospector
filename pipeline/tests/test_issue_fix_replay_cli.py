"""The command layer: assemble the pin's group, run each instance behind the
pool, and write one ledger row per instance plus a run summary and a markdown
table. The git leaves (screen, dep_declarations), the lane, and the store are
mocked, so only the orchestration runs — no network, no Docker, no real store."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline import profile, prove, storekit
from pipeline.evals import issue_fix_replay as replay


class FakeStore:
    """A pipeline.store.Store stand-in whose append_run validates each row the
    way the real ledger does, and whose runs() replays them typed."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append_run(self, record: dict) -> None:
        storekit.parse_run(record)
        self.rows.append(record)

    def runs(self, limit: int | None = None, since: str | None = None
             ) -> list[storekit.RunRecord]:
        rows = self.rows if limit is None else self.rows[-limit:]
        return [storekit.parse_run(r) for r in rows]


def _base(tmp_path: Path) -> prove.PinnedBase:
    return prove.PinnedBase(sha="e" * 40, tier=0, image="img", clone=tmp_path / "clone")


def _inst(issue: int, pr: int) -> replay.Instance:
    return replay.Instance(issue=issue, pr=pr, merge_sha="m" * 40, landed_diff="",
                           test_files=["tests/test_x.py"], nontest_files=["src/x.py"],
                           report_title="T", report_body="B")


def _rec(issue: int, pr: int, *, ending: str = "fixed", fixed: bool = True,
         oracle_pass: bool = True, reproduced: bool = True, repro_valid: bool = True,
         false_accept: bool = False, false_reject: bool = False, seconds: float = 10.0,
         agent_runs: int = 3) -> dict:
    return {"issue": issue, "pr": pr, "ending": ending, "seconds": seconds,
            "oracle_runs": {}, "reproduced": reproduced, "fixed": fixed,
            "repro_valid": repro_valid, "oracle_pass": oracle_pass, "oracle_coupled": False,
            "false_accept": false_accept, "false_reject": false_reject,
            "test_tamper": False, "localized": True, "agent_runs": agent_runs}


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    prof = profile.RepoProfile()
    monkeypatch.setattr(profile, "active", lambda: prof)
    monkeypatch.setattr(replay.settings, "verify_scratch", lambda: tmp_path)
    monkeypatch.setattr(replay.settings, "worker_id", lambda: "test-host")
    monkeypatch.setattr(replay, "dep_declarations", lambda base_clone, merge_sha, profile: {})
    store = FakeStore()
    monkeypatch.setattr(replay.store, "Store", lambda: store)
    return SimpleNamespace(base=_base(tmp_path), profile=prof, store=store, scratch=tmp_path)


def _mock_pipeline(monkeypatch: pytest.MonkeyPatch, instances: list[replay.Instance],
                   recs: dict[int, dict]) -> list[int]:
    """Route candidates_from_store → screen → run_instance to `instances`/`recs`,
    returning the issues run_instance was actually called for."""
    cands = [SimpleNamespace(issue=i.issue) for i in instances]
    monkeypatch.setattr(replay, "candidates_from_store", lambda *, limit=None: cands)
    by_issue = {i.issue: i for i in instances}
    monkeypatch.setattr(replay, "screen", lambda cand, **kw: (by_issue[cand.issue], None))
    calls: list[int] = []

    def fake_run_instance(inst: replay.Instance, *, base, base_sha, profile, workdir,
                          run_lane) -> dict:
        calls.append(inst.issue)
        return recs[inst.issue]

    monkeypatch.setattr(replay, "run_instance", fake_run_instance)
    return calls


def _instance_rows(store: FakeStore) -> list[dict]:
    return [r for r in store.rows if r["phase"] == "replay:instance"]


def _run_rows(store: FakeStore) -> list[dict]:
    return [r for r in store.rows if r["phase"] == "replay:run"]


def test_run_writes_one_instance_row_each_and_a_run_summary(wired, monkeypatch) -> None:
    insts = [_inst(7, 42), _inst(8, 43), _inst(9, 44)]
    recs = {7: _rec(7, 42),
            8: _rec(8, 43, ending="fix-rejected", fixed=False, false_reject=True),
            9: _rec(9, 44, oracle_pass=False, false_accept=True, seconds=5.0)}
    calls = _mock_pipeline(monkeypatch, insts, recs)

    rc = replay.run(wired.base, wired.base.sha, profile=wired.profile,
                    lane_logins=frozenset(), run_id="R1")

    assert rc == 0
    assert sorted(calls) == [7, 8, 9]
    inst_rows = _instance_rows(wired.store)
    run_rows = _run_rows(wired.store)
    assert len(inst_rows) == 3
    assert len(run_rows) == 1
    # Every appended row parses as a phase run and its stats carry the scores.
    for r in inst_rows:
        assert isinstance(storekit.parse_run(r), storekit.PhaseRun)
        assert {"fixed", "oracle_pass", "reproduced", "seconds"}.issubset(r["stats"])
    assert storekit.parse_run(run_rows[0]).phase == "replay:run"
    stats = run_rows[0]["stats"]
    assert stats["instances"] == 3
    assert stats["fixed"] == 2
    assert stats["oracle_pass"] == 2
    assert stats["false_accept"] == 1


def test_run_writes_a_markdown_table_with_a_row_per_instance_and_an_aggregate(
        wired, monkeypatch) -> None:
    insts = [_inst(7, 42), _inst(8, 43), _inst(9, 44)]
    recs = {i.issue: _rec(i.issue, i.pr) for i in insts}
    _mock_pipeline(monkeypatch, insts, recs)

    replay.run(wired.base, wired.base.sha, profile=wired.profile,
               lane_logins=frozenset(), run_id="R1")

    table = (wired.scratch / "replay" / "R1" / "table.md").read_text()
    rows = [ln for ln in table.splitlines() if ln.startswith("| ")]
    assert rows[0].startswith("| issue | pr |")
    data = rows[2:]  # after the header and the separator
    assert len(data) == 4  # three instances plus the aggregate line
    assert "total (3)" in data[-1]
    for issue in (7, 8, 9):
        assert any(ln.startswith(f"| {issue} |") for ln in data)


def test_limit_caps_the_instances(wired, monkeypatch) -> None:
    insts = [_inst(7, 42), _inst(8, 43), _inst(9, 44)]
    recs = {i.issue: _rec(i.issue, i.pr) for i in insts}
    calls = _mock_pipeline(monkeypatch, insts, recs)

    replay.run(wired.base, wired.base.sha, profile=wired.profile,
               lane_logins=frozenset(), limit=2, run_id="R1")

    assert set(calls) == {7, 8}
    assert len(_instance_rows(wired.store)) == 2


def _seed_instance(store: FakeStore, issue: int, *, ending: str) -> None:
    store.append_run({
        "phase": "replay:instance", "started": None, "finished": None, "trigger": "cli",
        "stats": {"run_id": "R1", "issue": issue, "pr": 42, "ending": ending,
                  "fixed": True, "oracle_pass": True, "reproduced": True,
                  "repro_valid": True, "false_accept": False, "false_reject": False,
                  "seconds": 3.0, "agent_runs": 2}})


def test_resume_skips_instances_already_recorded(wired, monkeypatch) -> None:
    insts = [_inst(7, 42), _inst(8, 43), _inst(9, 44)]
    recs = {i.issue: _rec(i.issue, i.pr) for i in insts}
    _seed_instance(wired.store, 7, ending="fixed")
    calls = _mock_pipeline(monkeypatch, insts, recs)

    replay.run(wired.base, wired.base.sha, profile=wired.profile,
               lane_logins=frozenset(), resume=True, run_id="R1")

    assert set(calls) == {8, 9}  # the recorded, non-fault issue 7 is not re-run
    fresh = [r for r in _instance_rows(wired.store) if r["stats"]["issue"] in (8, 9)]
    assert len(fresh) == 2
    # The reused issue 7 still lands in the table and the run total.
    assert _run_rows(wired.store)[0]["stats"]["instances"] == 3


def test_resume_reruns_a_faulted_instance(wired, monkeypatch) -> None:
    insts = [_inst(7, 42)]
    recs = {7: _rec(7, 42)}
    _seed_instance(wired.store, 7, ending="r6-sandbox")
    calls = _mock_pipeline(monkeypatch, insts, recs)

    replay.run(wired.base, wired.base.sha, profile=wired.profile,
               lane_logins=frozenset(), resume=True, run_id="R1")

    assert calls == [7]  # a faulted recording is retried under --resume


def test_issues_narrows_the_batch(wired, monkeypatch) -> None:
    insts = [_inst(7, 42), _inst(8, 43), _inst(9, 44)]
    recs = {i.issue: _rec(i.issue, i.pr) for i in insts}
    calls = _mock_pipeline(monkeypatch, insts, recs)

    replay.run(wired.base, wired.base.sha, profile=wired.profile,
               lane_logins=frozenset(), issues={8, 9}, run_id="R1")

    assert set(calls) == {8, 9}


def test_resume_reruns_a_crashed_instance(wired, monkeypatch) -> None:
    insts = [_inst(7, 42)]
    recs = {7: _rec(7, 42)}
    _seed_instance(wired.store, 7, ending="error")
    calls = _mock_pipeline(monkeypatch, insts, recs)

    replay.run(wired.base, wired.base.sha, profile=wired.profile,
               lane_logins=frozenset(), resume=True, run_id="R1")

    assert calls == [7]


def test_without_resume_a_recorded_instance_is_reused_not_rerun(wired, monkeypatch) -> None:
    insts = [_inst(7, 42), _inst(8, 43)]
    recs = {i.issue: _rec(i.issue, i.pr) for i in insts}
    _seed_instance(wired.store, 7, ending="fixed")
    calls = _mock_pipeline(monkeypatch, insts, recs)

    replay.run(wired.base, wired.base.sha, profile=wired.profile,
               lane_logins=frozenset(), run_id="R1")

    assert calls == [8]


def test_run_survives_a_crashed_instance(wired, monkeypatch) -> None:
    insts = [_inst(7, 42), _inst(8, 43), _inst(9, 44)]
    recs = {7: _rec(7, 42), 9: _rec(9, 44)}
    cands = [SimpleNamespace(issue=i.issue) for i in insts]
    monkeypatch.setattr(replay, "candidates_from_store", lambda *, limit=None: cands)
    by_issue = {i.issue: i for i in insts}
    monkeypatch.setattr(replay, "screen", lambda cand, **kw: (by_issue[cand.issue], None))

    def fake_run_instance(inst, *, base, base_sha, profile, workdir, run_lane) -> dict:
        if inst.issue == 8:
            raise ValueError("patch conflict mid-score")
        return recs[inst.issue]

    monkeypatch.setattr(replay, "run_instance", fake_run_instance)

    rc = replay.run(wired.base, wired.base.sha, profile=wired.profile,
                    lane_logins=frozenset(), run_id="R1")

    assert rc == 0
    by_i = {r["stats"]["issue"]: r["stats"] for r in _instance_rows(wired.store)}
    assert len(by_i) == 3  # the crashed instance is still recorded
    assert by_i[8]["ending"] == "error"
    assert "fixed" not in by_i[8]  # scores absent for the crashed one
    assert by_i[7]["ending"] == "fixed" and by_i[9]["ending"] == "fixed"
    run_stats = _run_rows(wired.store)[0]["stats"]
    assert run_stats["instances"] == 3
    assert run_stats["fixed"] == 2  # the error instance counts as not-fixed
    table = (wired.scratch / "replay" / "R1" / "table.md").read_text()
    assert any(ln.startswith("| 8 |") and "error" in ln for ln in table.splitlines())


def test_run_returns_setup_error_on_no_instances(wired, monkeypatch) -> None:
    monkeypatch.setattr(replay, "candidates_from_store", lambda *, limit=None: [])
    assert replay.run(wired.base, wired.base.sha, profile=wired.profile,
                      lane_logins=frozenset(), run_id="R1") == 2
    assert _instance_rows(wired.store) == []


def test_plan_prints_groups_and_runs_no_instance(wired, monkeypatch, capsys) -> None:
    insts = [_inst(7, 42), _inst(8, 43)]
    cands = [SimpleNamespace(issue=i.issue) for i in insts]
    monkeypatch.setattr(replay, "candidates_from_store", lambda *, limit=None: cands)
    by_issue = {i.issue: i for i in insts}
    monkeypatch.setattr(replay, "screen", lambda cand, **kw: (by_issue[cand.issue], None))
    ran: list[int] = []
    monkeypatch.setattr(replay, "run_instance", lambda *a, **k: ran.append(1))

    replay.plan(wired.base, wired.base.sha, profile=wired.profile, lane_logins=frozenset())

    assert ran == []
    out = capsys.readouterr().out
    assert "group" in out.lower()
    assert "2 pass" in out


def _closed_meta(**over: object) -> dict:
    meta = {"title": "Boom", "body": "it crashes", "state": "closed",
            "state_reason": "completed", "author": "bob",
            "created_at": "2024-01-01T00:00:00Z", "last_edited_at": "2024-01-05T00:00:00Z",
            "updated_at": "2024-06-01T00:00:00Z"}
    meta.update(over)
    return meta


def _seed_closed_issues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                        specs: list[tuple[int, int, dict]]) -> None:
    """Seed each (issue, pr, meta) into one IssueStore, give each a merged github
    closer, and route candidates_from_store's store, PR index, and PR fetch to it."""
    from issue_triage import issue_store, pr_index
    store = issue_store.IssueStore(tmp_path)
    for number, pr, meta in specs:
        iss = store.create_issue(number, meta)
        iss.apply_facts(meta, github=[{"pr": pr, "state": "merged", "draft": False}])
    monkeypatch.setattr(issue_store, "IssueStore", lambda: store)
    monkeypatch.setattr(pr_index, "from_store", lambda: {})
    monkeypatch.setattr(replay, "_gh_pr", lambda number: {
        "merge_sha": "a" * 40, "author": "carol",
        "opened_at": replay._parse_dt("2024-02-01T00:00:00Z")})


def test_candidate_updated_at_is_the_last_edit_not_the_close_bump(tmp_path, monkeypatch) -> None:
    _seed_closed_issues(tmp_path, monkeypatch, [(7, 42, _closed_meta())])

    cands = replay.candidates_from_store()

    assert len(cands) == 1
    cand = cands[0]
    assert cand.issue == 7
    assert cand.closed_completed is True
    assert cand.reporter == "bob"
    # updated_at is the issue's last-edit time, unaffected by its close.
    assert cand.updated_at == replay._parse_dt("2024-01-05T00:00:00Z")
    assert cand.created_at == replay._parse_dt("2024-01-01T00:00:00Z")
    assert [(p.number, p.author, p.merged) for p in cand.closing_prs] == [(42, "carol", True)]


def test_candidate_updated_at_falls_back_to_creation_when_never_edited(tmp_path, monkeypatch) -> None:
    _seed_closed_issues(tmp_path, monkeypatch,
                        [(7, 42, _closed_meta(state_reason="not_planned", last_edited_at=None))])

    cand = replay.candidates_from_store()[0]

    assert cand.closed_completed is False  # not_planned is not a completed close
    assert cand.updated_at == replay._parse_dt("2024-01-01T00:00:00Z")


def test_candidates_limit_takes_the_newest_issues_first(tmp_path, monkeypatch) -> None:
    _seed_closed_issues(tmp_path, monkeypatch,
                        [(7, 42, _closed_meta()), (9, 44, _closed_meta())])

    cands = replay.candidates_from_store(limit=1)

    assert [c.issue for c in cands] == [9]  # newest issue first, then the cap


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        replay.main(["--help"])
    assert exc.value.code == 0


def test_main_run_forwards_the_flags(monkeypatch, tmp_path) -> None:
    base = _base(tmp_path)
    monkeypatch.setattr(replay.store, "Store", lambda: FakeStore())
    monkeypatch.setattr(replay.prove, "pinned", lambda store: base)
    monkeypatch.setattr(replay.profile, "active", lambda: profile.RepoProfile())
    seen: dict[str, object] = {}

    def fake_run(b, base_sha, *, profile, lane_logins, limit, concurrency, resume,
                 candidate_cap, run_id=None, issues=None) -> int:
        seen.update(base_sha=base_sha, limit=limit, concurrency=concurrency, resume=resume,
                    candidate_cap=candidate_cap, run_id=run_id, issues=issues)
        return 0

    monkeypatch.setattr(replay, "run", fake_run)
    rc = replay.main(["run", "--limit", "5", "--concurrency", "3", "--resume",
                      "--candidates", "50", "--run-id", "R9", "--issues", "7, 8"])
    assert rc == 0
    assert seen == {"base_sha": base.sha, "limit": 5, "concurrency": 3, "resume": True,
                    "candidate_cap": 50, "run_id": "R9", "issues": {7, 8}}


def test_main_run_refuses_issues_that_are_not_numbers(monkeypatch, tmp_path, capsys) -> None:
    base = _base(tmp_path)
    monkeypatch.setattr(replay.store, "Store", lambda: FakeStore())
    monkeypatch.setattr(replay.prove, "pinned", lambda store: base)
    monkeypatch.setattr(replay.profile, "active", lambda: profile.RepoProfile())
    monkeypatch.setattr(replay, "run", lambda *a, **k: pytest.fail("no run on a bad list"))

    assert replay.main(["run", "--issues", "7,eight"]) == 2
    assert "issue numbers" in capsys.readouterr().err
