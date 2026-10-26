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
from dataclasses import replace
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

    def runs(self, limit: int | None = None, since: str | None = None,
             phase: str | None = None) -> list[storekit.RunRecord]:
        rows = [r for r in self.rows if phase is None or r.get("phase") == phase]
        rows = rows if limit is None else rows[-limit:]
        return [storekit.parse_run(r) for r in rows]


@pytest.fixture
def ledger(monkeypatch) -> FakeStore:
    fake = FakeStore()
    monkeypatch.setattr(eval_set, "Store", lambda: fake)
    monkeypatch.setenv("TRIAGE_REPO", "acme/widgets")
    monkeypatch.setattr(profile, "active", lambda: profile.RepoProfile())
    return fake


def _base_row(sha: str, *verdicts: str) -> dict:
    return {"base_sha": sha, "span": "span", "fixes": len(verdicts), "qualified_at": "t",
            "sandbox": eval_set.sandbox_stamp(profile.RepoProfile()),
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


def test_the_stamp_follows_the_verify_section():
    base = profile.RepoProfile()
    other_cmd = replace(base, verify=replace(base.verify, compile_cmd="pnpm tsc"))
    other_tiers = replace(base, trusted_authors=("someone",))
    assert eval_set.sandbox_stamp(other_cmd) != eval_set.sandbox_stamp(base)
    assert eval_set.sandbox_stamp(other_tiers) == eval_set.sandbox_stamp(base)


def test_an_import_replaces_a_stale_row_with_a_current_one(ledger, tmp_path):
    eval_set.record_base({**_base_row("a" * 40, "fair"), "sandbox": "old"})
    path = tmp_path / "set.json"
    path.write_text(json.dumps({"bases": [_base_row("a" * 40, "fair", "fair"),
                                          {**_base_row("b" * 40, "fair"), "sandbox": "old"}]}))
    eval_set.import_file(path)
    eval_set.import_file(path)
    manifest = eval_set.load_manifest()
    assert [b["base_sha"] for b in manifest["bases"]] == ["a" * 40] and manifest["fair"] == 2
    assert len(ledger.rows) == 3


def test_a_base_from_another_sandbox_profile_is_stale(ledger):
    eval_set.record_base(_base_row("a" * 40, "fair"))
    eval_set.record_base({**_base_row("b" * 40, "fair", "fair"), "sandbox": "old"})
    unstamped = _base_row("c" * 40, "fair")
    del unstamped["sandbox"]
    eval_set.record_base(unstamped)
    manifest = eval_set.load_manifest()
    assert [b["base_sha"] for b in manifest["bases"]] == ["a" * 40]
    assert manifest["fair"] == 1 and manifest["stale"] == ["b" * 40, "c" * 40]


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


def test_build_requalifies_a_stale_base(builder):
    eval_set.record_base({**_base_row("a" * 40, "fair"), "sandbox": "old"})
    _build()
    assert builder["qualified"] == ["a" * 40, "c" * 40]
    assert eval_set.load_manifest()["stale"] == []


@pytest.fixture
def compiling(builder, monkeypatch):
    """`builder` under a profile with a compile command; the pristine compile's
    `(exit, tail)` per base image is set in the returned dict (default: pass)."""
    from pipeline import gates
    prof = profile.RepoProfile()
    prof = replace(prof, verify=replace(prof.verify, compile_cmd="pnpm tsc"))
    monkeypatch.setattr(profile, "active", lambda: prof)
    answers: dict = {}

    def run_phase(phase, image, *, pristine=False, **kw):
        assert phase == "compile" and pristine
        return answers.get(image, (gates.SENTINEL_PASS, ""))
    monkeypatch.setattr(verify_driver, "run_phase", run_phase)
    monkeypatch.setattr(prove, "held", lambda sha, tier: prove.PinnedBase(
        sha=sha, tier=tier, image=sha[:4], clone=Path("/nonexistent")))
    return answers


def test_a_base_that_cannot_compile_holds_no_fair_bug(builder, compiling):
    from pipeline import gates
    compiling["aaaa"] = (gates.SENTINEL_TEST_FAIL, "error TS2307: Cannot find module")
    _build()
    assert builder["qualified"] == ["c" * 40]
    rows = {b["base_sha"]: b for b in eval_set.load_manifest()["bases"]}
    assert {i["verdict"] for i in rows["a" * 40]["instances"]} == {"base-compile"}
    assert rows["a" * 40]["instances"][0]["detail"].startswith(f"exit {gates.SENTINEL_TEST_FAIL}")


def test_a_base_whose_compile_times_out_holds_no_fair_bug(builder, compiling):
    compiling["aaaa"] = (124, "phase compile timed out after 1800s")
    _build()
    rows = {b["base_sha"]: b for b in eval_set.load_manifest()["bases"]}
    assert {i["verdict"] for i in rows["a" * 40]["instances"]} == {"base-compile"}


def test_a_sandbox_fault_in_the_base_check_records_nothing(builder, compiling):
    compiling["aaaa"] = (125, "docker: daemon not running")
    _build()
    assert builder["qualified"] == ["c" * 40]
    assert [b["base_sha"] for b in eval_set.load_manifest()["bases"]] == ["c" * 40]


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

    def run_instance(inst, *, base, base_sha, profile, workdir, run_lane, judge_contract, **kw):
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

    def run_instance(inst, *, base, base_sha, profile, workdir, run_lane, judge_contract, **kw):
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

    def run_instance(inst, *, base, base_sha, profile, workdir, run_lane, judge_contract, **kw):
        ran.append(inst.issue)
        return {"issue": inst.issue, "pr": inst.pr, "ending": "agent-unavailable",
                "detail": "You've hit your weekly limit", "agent_runs": 1}

    monkeypatch.setattr(replay, "run_instance", run_instance)
    assert eval_set.run(name="h", passes=2, concurrency=1, refresh=False, issues=None,
                        resume=False) == 3
    assert len(ran) == 1  # three bugs x two passes were queued
    card = [r["stats"] for r in ledger.rows if r["phase"] == eval_set.RUN_PHASE][-1]
    assert "weekly limit" in card["halted"]


def _craft_rec(result, *, sha="s1", points=(), flags=(), error=None, ending="fixed") -> dict:
    craft = None if result is ... else {"result": result, "points": list(points),
                                        "reasons": [], "judge_sha": "j", "error": error}
    return {"issue": 1, "ending": ending, "fixer": {"prompt_sha": sha, "models": "opus"},
            "craft": craft, "flags": list(flags)}


def test_scorecard_counts_craft_by_configuration():
    recs = [_craft_rec("win", flags=["foreign-text"]), _craft_rec("tie", points=[2]),
            _craft_rec("loss", points=[2, 4]), _craft_rec(None, error="boom"),
            _craft_rec(..., ending="no-fix"), _craft_rec("loss", sha="s2", points=[1])]
    craft = eval_set.scorecard(recs, bugs=1)["craft"]
    one = craft["s1/opus"]
    assert (one["judged"], one["wins"], one["ties"], one["losses"], one["errors"]) == (3, 1, 1, 1, 1)
    assert one["rate"] == 0.5
    assert one["losses_by_point"] == {2: 2, 4: 1}
    assert one["flags"] == {"foreign-text": 1}
    assert craft["s2/opus"]["losses"] == 1


def test_rate_is_none_when_nothing_was_judged():
    craft = eval_set.scorecard([_craft_rec(..., ending="no-fix")], bugs=1)["craft"]
    assert craft["s1/opus"]["rate"] is None and craft["s1/opus"]["judged"] == 0


def test_scorecard_md_names_the_points_lost_on():
    from pipeline.evals import craft_judge
    recs = [_craft_rec("loss", points=[2]), _craft_rec("loss", points=[2, 3])]
    md = eval_set._scorecard_md("n", eval_set.scorecard(recs, bugs=1), recs)
    assert "## Craft" in md and "Losing on" in md
    assert craft_judge.POINTS[1][:30] in md


def test_rows_without_a_fixer_add_no_craft_section():
    recs = [{"issue": 1, "ending": "r6-red"}, _craft_rec("win")]
    assert list(eval_set.scorecard(recs, bugs=1)["craft"]) == ["s1/opus"]


# --- rival and case-study cases ----------------------------------------------


def _case(issue: int = 9, *, source: str = "rival", oracle: str = "craft-only",
          detail: str = "", pr: int = 90) -> eval_set.Case:
    return eval_set.Case(
        case=f"{'rival' if source == 'rival' else 'case'}-{issue}", source=source,  # type: ignore[arg-type]
        issue=issue, pr=pr, merge_sha="m" * 40, base_sha="b" * 40, oracle=oracle,  # type: ignore[arg-type]
        detail=detail, report_title="t", report_body="b", landed_diff="diff --git a/x b/x\n",
        test_files=[], nontest_files=["x"])


def test_cases_round_trip_latest_per_case_wins(ledger):
    eval_set.record_case(_case(9, detail="first"))
    eval_set.record_case(_case(8))
    eval_set.record_case(_case(9, detail="second"))
    got = eval_set.load_cases()
    assert [c.case for c in got] == ["rival-8", "rival-9"]
    assert got[1].detail == "second"


def test_cases_from_another_repo_are_ignored(ledger, monkeypatch):
    eval_set.record_case(_case(9))
    monkeypatch.setenv("TRIAGE_REPO", "other/repo")
    assert eval_set.load_cases() == []


def test_case_instance_carries_its_inputs():
    inst = _case(9).instance()
    assert (inst.issue, inst.pr, inst.merge_sha) == (9, 90, "m" * 40)
    assert inst.pre_fix == "m" * 40 + "^1"
    assert replace(_case(9), pre_sha="f" * 40).instance().pre_fix == "f" * 40
    assert inst.landed_diff.startswith("diff --git") and inst.nontest_files == ["x"]
    assert (inst.report_title, inst.report_body) == ("t", "b")


class _Issues:
    def __init__(self, rows: list[tuple[dict, list]]) -> None:
        self.rows = rows

    def issue_rows(self, paths, **kw):
        return self.rows


def test_rival_candidates_from_choices_and_gaps():
    rows = [({"issue": 1}, [{"pr": 11}, None]),
            ({"issue": 2}, [None, [{"pr": 21, "verdict": "gap-open"},
                                   {"pr": 22, "verdict": "gap-closed"},
                                   {"pr": 23, "verdict": "covered"}, {"pr": 24, "verdict": "gap"}]]),
            ({"issue": 3}, [None, None])]
    assert eval_set.rival_candidates(_Issues(rows)) == [(1, 11), (2, 21), (2, 24)]  # type: ignore[arg-type]


LANDED = ("diff --git a/src/a.ts b/src/a.ts\n--- a/src/a.ts\n+++ b/src/a.ts\n@@ -1 +1 @@\n-a\n+b\n")
LANDED_TESTS = LANDED + ("diff --git a/src/a.test.ts b/src/a.test.ts\n--- a/src/a.test.ts\n"
                         "+++ b/src/a.test.ts\n@@ -1 +1 @@\n-x\n+y\n")


@pytest.fixture
def qualifying(monkeypatch, tmp_path):
    """The pin, gh and git faked; returns knobs the test sets."""
    knobs = {"merged": True, "landed": LANDED, "r6": (True, ""), "flatten": True, "open": None,
             "pre_fix": []}
    monkeypatch.setattr(replay, "_gh_pr", lambda pr: {"merge_sha": "m" * 40, "author": "a",
                                                      "opened_at": None}
                        if knobs["merged"] else None)

    def git(repo, *args):
        if knobs["landed"] is None:
            raise subprocess.CalledProcessError(128, ["git"], stderr="bad object")
        return knobs["landed"]
    monkeypatch.setattr(replay, "_git", git)
    monkeypatch.setattr(eval_set, "_open_rival", lambda pr: knobs["open"])

    def transform_to_p(clone, pre_fix, base_sha, prof):
        knobs["pre_fix"].append(pre_fix)
        return "PRE"
    monkeypatch.setattr(replay, "transform_to_p", transform_to_p)
    monkeypatch.setattr(replay, "validate_known_fix", lambda *a, **k: knobs["r6"])

    knobs["flattened"] = []

    def flatten(clone, *patches, **k):
        knobs["flattened"].append(patches)
        if not knobs["flatten"]:
            raise ValueError("does not apply")
        return tmp_path / "p.diff"
    monkeypatch.setattr(prove, "flatten", flatten)
    knobs["pin"] = prove.PinnedBase(sha="p" * 40, tier=1, image="i", clone=tmp_path)
    knobs["deps"] = {}
    monkeypatch.setattr(replay, "dep_declarations",
                        lambda clone, rev, prof: knobs["deps"].get(rev, {"x": "1"}))
    knobs["held"] = []

    def held_base(sha):
        knobs["held"].append(sha)
        return knobs["pin"]
    monkeypatch.setattr(eval_set, "_held_base", held_base)
    return knobs


def _qualify(knobs, source="rival"):
    return eval_set.qualify_case(source, 9, 90, pin=knobs["pin"], report=("t", "b"),
                                 prof=profile.active(), run_r6=True)


def test_a_rival_closed_unmerged_is_unusable(qualifying):
    qualifying["merged"] = False
    case = _qualify(qualifying)
    assert case.oracle == "unusable" and "neither merged nor open" in case.detail


def test_an_open_rival_is_craft_only_on_the_pin(qualifying):
    qualifying["merged"] = False
    qualifying["r6"] = None  # an open PR's tests are no oracle, so R6 never runs
    qualifying["open"] = eval_set.OpenPr(head="h" * 40, diff=LANDED_TESTS)
    case = _qualify(qualifying)
    assert (case.oracle, case.merge_sha, case.pre_sha, case.head_sha) == (
        "craft-only", "", "p" * 40, "h" * 40)
    assert case.landed_diff == LANDED_TESTS and case.test_files == ["src/a.test.ts"]
    assert qualifying["pre_fix"] == ["p" * 40]
    assert qualifying["flattened"][-1] == ("PRE", LANDED_TESTS)


def test_a_rival_github_refuses_is_retried_not_written_off(qualifying):
    qualifying["merged"] = False
    qualifying["open"] = "GitHub refused the read"
    case = _qualify(qualifying)
    assert case.oracle == "unusable" and "re-harvest to retry" in case.detail


def test_an_open_rival_whose_diff_misses_the_pin_is_unusable(qualifying):
    qualifying["merged"] = False
    qualifying["open"] = eval_set.OpenPr(head="h" * 40, diff=LANDED)
    qualifying["flatten"] = False
    assert _qualify(qualifying).oracle == "unusable"


def test_a_merged_rival_is_rebuilt_from_the_merge_first_parent(qualifying):
    _qualify(qualifying)
    assert qualifying["pre_fix"] == ["m" * 40 + "^1"]


def test_open_rival_reads_the_diff_at_the_head(monkeypatch):
    from pipeline import gh
    prs = {5: {"state": "open", "base": {"sha": "B"}, "head": {"sha": "H"}},
           6: {"state": "closed", "base": {"sha": "B"}, "head": {"sha": "H"}},
           8: {"state": "open", "base": {"sha": "B"}, "head": {"sha": "TOO-BIG"}}}
    monkeypatch.setattr(gh, "fetch_pr", lambda n, **k: prs.get(n))
    seen = []

    def gh_bytes(path, **k):
        seen.append(path)
        return None if "TOO-BIG" in path else b"diff --git a/x b/x\n"
    monkeypatch.setattr(gh, "gh_bytes", gh_bytes)
    assert eval_set._open_rival(5) == eval_set.OpenPr(head="H", diff="diff --git a/x b/x\n")
    assert seen[0].endswith("/compare/B...H")
    assert eval_set._open_rival(6) is None
    assert isinstance(eval_set._open_rival(7), str) and isinstance(eval_set._open_rival(8), str)


def test_a_case_row_from_before_open_rivals_still_loads(ledger):
    eval_set.record_case(_case(9))
    ledger.rows[0]["stats"].pop("pre_sha")
    assert eval_set.load_cases()[0].pre_sha == ""


def test_a_merge_the_pin_lacks_is_not_in_pin(qualifying):
    qualifying["landed"] = None
    case = _qualify(qualifying)
    assert case.oracle == "unusable" and "not-in-pin" in case.detail


def test_a_rival_with_qualifying_tests_is_an_oracle_case(qualifying):
    qualifying["landed"] = LANDED_TESTS
    case = _qualify(qualifying)
    assert (case.oracle, case.case, case.base_sha) == ("tests", "rival-9-90", "p" * 40)
    assert case.test_files == ["src/a.test.ts"] and case.nontest_files == ["src/a.ts"]


def test_a_rival_without_tests_is_craft_only(qualifying):
    case = _qualify(qualifying)
    assert case.oracle == "craft-only" and case.landed_diff == LANDED


def test_a_pre_fix_tree_that_does_not_apply_is_unusable(qualifying):
    qualifying["flatten"] = False
    assert _qualify(qualifying).oracle == "unusable"


def test_dry_run_records_nothing(qualifying, ledger, monkeypatch):
    monkeypatch.setattr(eval_set, "rival_candidates", lambda issues: [(9, 90)])
    monkeypatch.setattr(eval_set, "_case_inputs", lambda: (qualifying["pin"], _Issues([]),
                                                           lambda n: ("t", "b")))
    held: list = []
    monkeypatch.setattr(verify_gc, "hold", lambda sha: held.append(sha))
    assert eval_set.harvest_cases(dry_run=True) == 0
    assert ledger.rows == [] and held == []
    assert eval_set.harvest_cases(dry_run=False) == 0
    assert [c.case for c in eval_set.load_cases()] == ["rival-9-90"] and held == ["p" * 40]


def test_add_case_is_a_case_study(qualifying, ledger, monkeypatch):
    monkeypatch.setattr(eval_set, "_case_inputs", lambda: (qualifying["pin"], _Issues([]),
                                                           lambda n: ("t", "b")))
    monkeypatch.setattr(verify_gc, "hold", lambda sha: None)
    assert eval_set.add_case(9, 90, dry_run=False) == 0
    [case] = eval_set.load_cases()
    assert (case.source, case.case) == ("case-study", "case-9-90")


def _fake_runner(ran: list):
    def run_instance(inst, *, base, base_sha, profile, workdir, run_lane, judge_contract,
                     oracle=True, **kw):
        ran.append((inst.issue, base_sha, oracle))
        return {"issue": inst.issue, "pr": inst.pr, "ending": "fixed",
                "oracle_pass": True if oracle else None, "craft_only": not oracle,
                "fixed": True, "seconds": 1.0, "agent_runs": 1}
    return run_instance


def test_run_replays_cases_beside_the_fair_bugs(builder, ledger, tmp_path, monkeypatch):
    monkeypatch.setattr(eval_set.settings, "verify_scratch", lambda: tmp_path / "vs")
    _build()
    eval_set.record_case(_case(7, oracle="tests"))
    eval_set.record_case(_case(8, oracle="unusable", detail="not merged"))
    ran: list = []
    monkeypatch.setattr(replay, "run_instance", _fake_runner(ran))
    assert eval_set.run(name="r", passes=1, concurrency=1, refresh=False, issues=None,
                        resume=False) == 0
    assert sorted(ran) == [(1, "a" * 40, True), (3, "a" * 40, True), (5, "c" * 40, True),
                           (7, "b" * 40, True)]
    stats = {s["case"]: s for s in (r["stats"] for r in ledger.rows
                                    if r["phase"] == "replay:instance")}
    assert stats["rival-7"]["source"] == "rival" and stats["1"]["source"] == "history"
    assert stats["rival-7"]["reference"] == "m" * 40


def test_an_open_case_records_the_head_it_was_judged_at(builder, ledger, tmp_path, monkeypatch):
    monkeypatch.setattr(eval_set.settings, "verify_scratch", lambda: tmp_path / "vs")
    _build()
    eval_set.record_case(replace(_case(7), merge_sha="", pre_sha="b" * 40, head_sha="h" * 40))
    monkeypatch.setattr(replay, "run_instance", _fake_runner([]))
    eval_set.run(name="r", passes=1, concurrency=1, refresh=False, issues={7}, resume=False)
    stats = [r["stats"] for r in ledger.rows if r["phase"] == "replay:instance"]
    assert [s["reference"] for s in stats] == ["h" * 40]


def test_an_issue_both_fair_and_rival_is_run_as_both(builder, ledger, tmp_path, monkeypatch):
    monkeypatch.setattr(eval_set.settings, "verify_scratch", lambda: tmp_path / "vs")
    _build()
    eval_set.record_case(_case(1, oracle="craft-only"))
    ran: list = []
    monkeypatch.setattr(replay, "run_instance", _fake_runner(ran))
    eval_set.run(name="r", passes=1, concurrency=1, refresh=False, issues={1}, resume=False)
    assert sorted(ran) == [(1, "a" * 40, True), (1, "b" * 40, False)]
    ran.clear()
    eval_set.run(name="r", passes=1, concurrency=1, refresh=False, issues={1}, resume=False)
    assert ran == []  # both resumed by case id


def test_craft_only_records_leave_precision_alone():
    records = [{"issue": 1, "case": "1", "ending": "fixed", "oracle_pass": True},
               {"issue": 2, "case": "rival-2-9", "source": "rival", "ending": "fixed",
                "oracle_pass": None, "craft_only": True}]
    card = eval_set.scorecard(records, bugs=1)
    assert (card["proposed"], card["correct"], card["precision"], card["coverage"]) == (
        1, 1, 1.0, 1.0)
    assert card["cases"] == {"history": 1, "rival": 1}


def test_unusable_cases_are_not_run(builder, ledger, tmp_path, monkeypatch):
    monkeypatch.setattr(eval_set.settings, "verify_scratch", lambda: tmp_path / "vs")
    _build()
    eval_set.record_case(_case(8, oracle="unusable", detail="not merged"))
    ran: list = []
    monkeypatch.setattr(replay, "run_instance", _fake_runner(ran))
    eval_set.run(name="r", passes=1, concurrency=1, refresh=False, issues={8}, resume=False)
    assert ran == []



def test_a_case_whose_deps_drifted_from_the_pin_is_unusable(qualifying):
    qualifying["deps"] = {"m" * 40 + "^1": {"x": "0"}}
    case = _qualify(qualifying)
    assert case.oracle == "unusable" and "deps drifted" in case.detail


def test_a_sandbox_fault_in_r6_does_not_downgrade_the_case(qualifying):
    qualifying["landed"] = LANDED_TESTS
    qualifying["r6"] = (False, "sandbox")
    case = _qualify(qualifying)
    assert case.oracle == "unusable" and "sandbox" in case.detail


def test_a_case_without_report_text_is_unusable(qualifying):
    case = eval_set.qualify_case("case-study", 9, 90, pin=qualifying["pin"], report=("", ""),
                                 prof=profile.active(), run_r6=True)
    assert case.oracle == "unusable" and "report" in case.detail


def test_a_real_harvest_qualifies_on_the_tier_1_base_and_records_once(qualifying, ledger,
                                                                     monkeypatch):
    monkeypatch.setattr(eval_set, "rival_candidates", lambda issues: [(9, 90)])
    monkeypatch.setattr(eval_set, "_case_inputs", lambda: (qualifying["pin"], _Issues([]),
                                                           lambda n: ("t", "b")))
    holds: list = []
    monkeypatch.setattr(verify_gc, "hold", lambda sha: holds.append(sha))
    eval_set.harvest_cases(dry_run=False)
    eval_set.harvest_cases(dry_run=False)
    assert qualifying["held"] == ["p" * 40, "p" * 40]
    assert len([r for r in ledger.rows if r["phase"] == eval_set.CASE_PHASE]) == 1
    assert holds == ["p" * 40]


def test_only_a_usable_case_holds_the_base(qualifying, ledger, monkeypatch):
    qualifying["merged"] = False
    monkeypatch.setattr(eval_set, "_case_inputs", lambda: (qualifying["pin"], _Issues([]),
                                                           lambda n: ("t", "b")))
    holds: list = []
    monkeypatch.setattr(verify_gc, "hold", lambda sha: holds.append(sha))
    eval_set.add_case(9, 90, dry_run=False)
    assert holds == [] and eval_set.load_cases()[0].oracle == "unusable"


def test_a_case_whose_base_cannot_be_built_leaves_the_run_going(builder, ledger, tmp_path,
                                                                monkeypatch):
    monkeypatch.setattr(eval_set.settings, "verify_scratch", lambda: tmp_path / "vs")
    _build()
    eval_set.record_case(replace(_case(7, oracle="tests"), base_sha="z" * 40))
    real = eval_set._held_base

    def held(sha):
        if sha == "z" * 40:
            raise verify_driver.BuildFailure("lockfile no longer resolves")
        return real(sha)
    monkeypatch.setattr(eval_set, "_held_base", held)
    ran: list = []
    monkeypatch.setattr(replay, "run_instance", _fake_runner(ran))
    assert eval_set.run(name="r", passes=1, concurrency=1, refresh=False, issues=None,
                        resume=False) == 0
    assert sorted(i for i, _, _ in ran) == [1, 3, 5]


def test_unusable_is_a_fault():
    assert replay._is_fault("unusable")


def test_a_run_names_the_stale_bases_it_leaves_out(ledger, capsys):
    eval_set.record_base({**_base_row("b" * 40, "fair"), "sandbox": "old"})
    assert eval_set.run(name="r", passes=1, concurrency=1, refresh=False, issues=None,
                        resume=False, cases=False) == 2
    err = capsys.readouterr()
    assert "another sandbox profile" in err.out + err.err and "bbbbbbbbbbbb" in err.out + err.err
