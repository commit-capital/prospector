"""The evaluation-set builder: harvested issues and PRs become screening
candidates, a group's epoch is the first later commit with its dependencies, and
a build adds one held, qualified base per group and resumes past the ones the
manifest names. GitHub, the base build and R6 are mocked; the epoch walk runs
over a real git history."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline import profile, prove, storekit, verify_driver, verify_gc
from pipeline.evals import eval_set
from pipeline.evals import issue_fix_replay as replay

_GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost"}


def _pr(number: int, body: str = "", sha: str | None = None) -> dict:
    return {"number": number, "title": f"PR {number}", "body": body,
            "createdAt": "2026-06-02T00:00:00Z", "author": {"login": "dev"},
            "mergeCommit": {"oid": sha or f"{number:040d}"}}


def _issue(number: int, closer: dict | None = None, reason: str = "COMPLETED") -> dict:
    return {"number": number, "stateReason": reason, "title": f"Bug {number}",
            "body": "It breaks.", "createdAt": "2026-06-01T00:00:00Z", "lastEditedAt": None,
            "author": {"login": "reporter"},
            "timelineItems": {"nodes": [{"closer": closer}] if closer else []}}


# --- harvest -> candidates ---------------------------------------------------


def test_a_merged_pr_saying_fixes_names_the_issue_s_fix():
    raw = {"issues": [_issue(5)], "prs": [_pr(10, "This fixes #5.")]}
    (cand,) = eval_set.candidates(raw)
    assert cand.issue == 5 and cand.closed_completed
    assert [p.number for p in cand.closing_prs] == [10]
    assert cand.closing_prs[0].merge_sha == f"{10:040d}"
    assert cand.reporter == "reporter" and cand.report_title == "Bug 5"


def test_github_s_recorded_closer_names_the_fix_directly_or_through_its_commit():
    raw = {"issues": [_issue(5, {"__typename": "PullRequest", "number": 10}),
                      _issue(6, {"__typename": "Commit",
                                 "associatedPullRequests": {"nodes": [{"number": 11}]}})],
           "prs": [_pr(10), _pr(11)]}
    got = {c.issue: [p.number for p in c.closing_prs] for c in eval_set.candidates(raw)}
    assert got == {5: [10], 6: [11]}


def test_an_issue_naming_no_merged_fix_is_no_candidate():
    raw = {"issues": [_issue(5, {"__typename": "PullRequest", "number": 99}), _issue(6)],
           "prs": [_pr(10, "mentions #6 in passing")]}
    assert eval_set.candidates(raw) == []


def test_an_issue_closed_as_not_planned_is_kept_for_the_screen_to_refuse():
    raw = {"issues": [_issue(5, reason="NOT_PLANNED")], "prs": [_pr(10, "closes #5")]}
    (cand,) = eval_set.candidates(raw)
    assert cand.closed_completed is False


def _cand(issue: int, prs: list[int], title: str = "t") -> replay.Candidate:
    when = datetime(2026, 6, 1, tzinfo=UTC)
    return replay.Candidate(
        issue=issue, closed_completed=True,
        closing_prs=[replay.ClosingPr(number=p, merged=True, merge_sha=f"{p:040d}",
                                      author="dev", opened_at=when) for p in prs],
        reporter="r", created_at=when, updated_at=when, report_title=title, report_body="b")


def test_the_union_keeps_the_first_source_s_report_and_every_source_s_fixes():
    merged = eval_set.union([_cand(5, [10], "harvested"), _cand(6, [12])],
                            [_cand(5, [11], "stored"), _cand(7, [13])])
    assert [c.issue for c in merged] == [5, 6, 7]
    assert merged[0].report_title == "harvested"
    assert [p.number for p in merged[0].closing_prs] == [10, 11]


# --- epoch ------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    env = {**{k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}, **_GIT_ENV}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout


def _commit(repo: Path, deps: dict[str, str], note: str) -> str:
    (repo / "package.json").write_text(json.dumps({"dependencies": deps}))
    (repo / "notes.txt").write_text(note)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-gpg-sign", "-m", note)
    return _git(repo, "rev-parse", "HEAD").strip()


def _inst(merge_sha: str, pr: int = 1) -> replay.Instance:
    return replay.Instance(issue=pr, pr=pr, merge_sha=merge_sha, landed_diff="",
                           test_files=[], nontest_files=[], report_title="", report_body="")


@pytest.fixture
def history(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def test_the_epoch_is_the_first_later_commit_with_the_group_s_dependencies(history):
    first = _commit(history, {"a": "1"}, "fix one")
    last = _commit(history, {"a": "1"}, "fix two")
    after = _commit(history, {"a": "1"}, "unrelated work")
    _commit(history, {"a": "2"}, "dependency bump")
    key = frozenset({("a", "1")})
    head = _git(history, "rev-parse", "HEAD").strip()
    got = eval_set.epoch(history, [_inst(first, 1), _inst(last, 2)], key, head,
                         profile.RepoProfile())
    assert got == after


def test_the_epoch_falls_back_to_the_last_merge_when_the_dependencies_move_at_once(history):
    last = _commit(history, {"a": "1"}, "the fix")
    _commit(history, {"a": "2"}, "dependency bump")
    head = _git(history, "rev-parse", "HEAD").strip()
    got = eval_set.epoch(history, [_inst(last)], frozenset({("a", "1")}), head,
                         profile.RepoProfile())
    assert got == last


# --- the set in the ledger ---------------------------------------------------


class FakeStore:
    """A pipeline.store.Store stand-in whose ledger validates each row the way
    the real one does."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append_run(self, record: dict) -> None:
        storekit.parse_run(record)
        self.rows.append(record)

    def runs(self, limit: int | None = None, since: str | None = None
             ) -> list[storekit.RunRecord]:
        rows = self.rows if limit is None else self.rows[-limit:]
        return [storekit.parse_run(r) for r in rows]


@pytest.fixture
def ledger(monkeypatch) -> FakeStore:
    fake = FakeStore()
    monkeypatch.setattr(eval_set, "Store", lambda: fake)
    monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
    return fake


def _base_row(sha: str, *verdicts: str) -> dict:
    return {"base_sha": sha, "span": "span", "fixes": len(verdicts), "qualified_at": "t",
            "instances": [{"issue": n + 1, "pr": n + 1, "verdict": v, "detail": ""}
                          for n, v in enumerate(verdicts)]}


def test_the_manifest_is_the_ledger_s_latest_row_per_base_for_this_repository(ledger):
    eval_set.record_base(_base_row("a" * 40, "fair", "r6-red"))
    eval_set.record_base(_base_row("b" * 40, "fair"))
    eval_set.record_base(_base_row("a" * 40, "fair", "fair"))
    ledger.rows.append({"phase": eval_set.BASE_PHASE, "started": "t", "finished": "t",
                        "stats": {**_base_row("c" * 40, "fair"), "repo": "other/repo"}})
    manifest = eval_set.load_manifest()
    assert [b["base_sha"] for b in manifest["bases"]] == ["b" * 40, "a" * 40]
    assert manifest["fair"] == 3


def test_an_import_records_only_the_bases_the_ledger_lacks(ledger, tmp_path):
    eval_set.record_base(_base_row("a" * 40, "fair"))
    path = tmp_path / "set.json"
    path.write_text(json.dumps({"bases": [_base_row("a" * 40, "fair"),
                                          _base_row("b" * 40, "fair")]}))
    eval_set.import_file(path)
    assert [r["stats"]["base_sha"] for r in ledger.rows] == ["a" * 40, "b" * 40]


# --- build ------------------------------------------------------------------


@pytest.fixture
def builder(tmp_path, monkeypatch, ledger):
    """A build over two groups, with GitHub, the pin, the base build and R6 mocked;
    returns the calls it made."""
    monkeypatch.setattr(verify_gc, "HELD_FILE", tmp_path / "held-bases")
    monkeypatch.setattr(profile, "active", lambda: profile.RepoProfile())
    pin = prove.PinnedBase(sha="p" * 40, tier=1, image="img", clone=tmp_path / "pin")
    monkeypatch.setattr(prove, "pinned", lambda store: pin)
    monkeypatch.setattr(eval_set, "harvest", lambda refresh=False: {"issues": [], "prs": []})
    monkeypatch.setattr(replay, "candidates_from_store", lambda limit=None: [])
    big = [_inst("a" * 40, 1), _inst("a" * 40, 2), _inst("b" * 40, 3)]
    small = [_inst("c" * 40, 4), _inst("d" * 40, 5)]
    monkeypatch.setattr(replay, "_screen", lambda *a, **k: (big + small, replay.Counter()))
    monkeypatch.setattr(replay, "group_by_deps", lambda instances, clone, prof: {
        frozenset({("x", "1")}): small, frozenset({("x", "2")}): big})
    monkeypatch.setattr(replay, "_date_span", lambda clone, members: "span")
    monkeypatch.setattr(eval_set, "epoch",
                        lambda clone, members, key, head, prof: members[0].merge_sha)
    calls: dict = {"built": [], "qualified": [], "lane": []}
    present: set[str] = set()

    def held(sha, tier):
        if sha not in present:
            raise prove.NoBase("absent")
        return prove.PinnedBase(sha=sha, tier=tier, image="img", clone=tmp_path / sha[:4])

    def build_base_image(sha, *, tier):
        calls["built"].append(sha)
        present.add(sha)
        return "img"

    def qualify(base, sha, members, *, profile, concurrency=2):
        calls["qualified"].append(sha)
        return [replay.Qualification(m.issue, m.pr, "fair" if m.issue % 2 else "r6-red",
                                     1.0, "") for m in members]

    monkeypatch.setattr(prove, "held", held)
    monkeypatch.setattr(verify_driver, "build_base_image", build_base_image)
    monkeypatch.setattr(replay, "qualify_instances", qualify)
    return calls


def _build(**over) -> int:
    args = dict(target=40, min_fixes=1, refresh=False, concurrency=1, dry_run=False)
    args.update(over)
    return eval_set.build(**args)  # type: ignore[arg-type]


def test_a_build_holds_builds_and_qualifies_one_base_per_group_most_fixes_first(builder):
    _build()
    assert builder["built"] == ["a" * 40, "c" * 40]
    assert verify_gc.held_shas() == frozenset({"a" * 12, "c" * 12})
    manifest = eval_set.load_manifest()
    assert [b["base_sha"] for b in manifest["bases"]] == ["a" * 40, "c" * 40]
    assert manifest["fair"] == 3  # issues 1, 3 and 5
    assert manifest["bases"][0]["instances"][0] == {"issue": 1, "pr": 1, "verdict": "fair",
                                                    "detail": ""}


def test_a_build_resumes_past_the_bases_the_manifest_names(builder):
    _build(target=1)
    assert builder["qualified"] == ["a" * 40]
    _build()
    assert builder["qualified"] == ["a" * 40, "c" * 40]


def test_a_dry_run_builds_nothing(builder):
    _build(dry_run=True)
    assert builder["built"] == [] and builder["qualified"] == []
    assert eval_set.load_manifest()["bases"] == []


def test_groups_below_the_minimum_fixes_are_left_out(builder):
    _build(min_fixes=3)
    assert builder["qualified"] == ["a" * 40]


# --- run and scorecard --------------------------------------------------------


def test_the_scorecard_reads_precision_over_proposals_and_coverage_over_bugs():
    records = [
        {"issue": 1, "ending": "fixed", "oracle_pass": True, "agent_runs": 5, "seconds": 10.0},
        {"issue": 1, "ending": "fixed", "oracle_pass": False, "contract_mismatch": True},
        {"issue": 2, "ending": "fixed", "oracle_pass": False},
        {"issue": 2, "ending": "not-a-defect"},
        {"issue": 3, "ending": "agent-unavailable"},
    ]
    card = eval_set.scorecard(records, bugs=3)
    assert (card["runs"], card["scored"], card["faults"]) == (5, 4, 1)
    assert (card["proposed"], card["correct"]) == (3, 2)
    assert card["precision"] == 0.667
    assert card["coverage"] == 0.667  # bugs 1 and 2 got a proposal
    assert card["bugs_correct"] == 1


def test_a_run_replays_each_fair_bug_once_per_pass_and_resumes(builder, ledger, tmp_path,
                                                                monkeypatch):
    monkeypatch.setattr(eval_set.settings, "verify_scratch", lambda: tmp_path / "vs")
    _build()
    ran: list[tuple[int, str]] = []

    def run_instance(inst, *, base, base_sha, profile, workdir, run_lane, judge_contract):
        ran.append((inst.issue, base_sha))
        return {"issue": inst.issue, "pr": inst.pr, "ending": "fixed", "oracle_pass": True,
                "seconds": 1.0, "agent_runs": 4}

    monkeypatch.setattr(replay, "run_instance", run_instance)
    assert eval_set.run(name="base", passes=2, concurrency=1, refresh=False, issues=None,
                        resume=False) == 0
    assert sorted(ran) == [(1, "a" * 40), (1, "a" * 40), (3, "a" * 40), (3, "a" * 40),
                           (5, "c" * 40), (5, "c" * 40)]
    runs = [r["stats"] for r in ledger.rows if r["phase"] == "replay:instance"]
    assert sorted({s["run_id"] for s in runs}) == ["base-p1", "base-p2"]
    card = [r["stats"] for r in ledger.rows if r["phase"] == eval_set.RUN_PHASE][-1]
    assert (card["proposed"], card["precision"], card["coverage"]) == (6, 1.0, 1.0)
    assert (tmp_path / "vs" / "replay" / "base" / "scorecard.md").exists()

    ran.clear()
    eval_set.run(name="base", passes=2, concurrency=1, refresh=False, issues=None,
                 resume=False)
    assert ran == []


def test_a_run_measures_the_lane_it_names(builder, ledger, tmp_path, monkeypatch):
    monkeypatch.setattr(eval_set.settings, "verify_scratch", lambda: tmp_path / "vs")
    _build()
    lanes: set = set()

    def run_instance(inst, *, base, base_sha, profile, workdir, run_lane, judge_contract):
        lanes.add(run_lane)
        return {"issue": inst.issue, "pr": inst.pr, "ending": "no-fix", "agent_runs": 1,
                "lane": {"reviews": []}}

    monkeypatch.setattr(replay, "run_instance", run_instance)
    eval_set.run(name="solo", passes=1, concurrency=1, refresh=False, issues=None,
                 resume=False, lane="solo")
    assert lanes == {replay._run_solo}
    card = [r["stats"] for r in ledger.rows if r["phase"] == eval_set.RUN_PHASE][-1]
    assert card["lane"] == "solo"
    instance = [r["stats"] for r in ledger.rows if r["phase"] == "replay:instance"]
    assert instance and all(st["eval_lane"] == "solo" for st in instance)
    assert all(st["lane"] == {"reviews": []} for st in instance)


def test_a_run_halts_when_the_agent_can_serve_nothing(builder, ledger, tmp_path, monkeypatch):
    monkeypatch.setattr(eval_set.settings, "verify_scratch", lambda: tmp_path / "vs")
    _build()
    ran: list[int] = []

    def run_instance(inst, *, base, base_sha, profile, workdir, run_lane, judge_contract):
        ran.append(inst.issue)
        return {"issue": inst.issue, "pr": inst.pr, "ending": "agent-unavailable",
                "detail": "You've hit your weekly limit", "agent_runs": 1}

    monkeypatch.setattr(replay, "run_instance", run_instance)
    assert eval_set.run(name="h", passes=2, concurrency=1, refresh=False, issues=None,
                        resume=False) == 3
    assert len(ran) == 1  # three bugs x two passes were queued
    card = [r["stats"] for r in ledger.rows if r["phase"] == eval_set.RUN_PHASE][-1]
    assert "weekly limit" in card["halted"]
