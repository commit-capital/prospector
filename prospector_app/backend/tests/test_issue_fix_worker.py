"""The issue-fix review lane: which requests a host takes, the replies it reads
from GitHub, the hunter's picks, and the requests it recovers from a worker
that is gone."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from issue_triage import dispute_question, feature_history, feature_screen, fix_review, fix_review_runner, fixability, public_loop
from issue_triage.issue_store import IssueStore
from pipeline import headless_agent, profile, prove, settings
from pipeline import store as S
from prospector_app.backend import data, issue_data, issue_fix_worker, lane_health

QUESTION = {"question": "2 or 3?", "options": [{"label": "A", "behavior": "2"},
                                               {"label": "B", "behavior": "3"}],
            "default": "A", "default_reason": "r"}


def _issue(store: IssueStore, n: int, *, created: str = "2026-09-20T00:00:00Z",
           grade: str = "A", labels: list[str] | None = None) -> None:
    store.save_issue({"issue": n, "meta": {"title": f"bug {n}", "state": "open",
                                           "updated_at": created, "created_at": created,
                                           "body": "b", "author": "reporter",
                                           "labels": labels or []},
                      "repro": {"grade": grade}})


@pytest.fixture
def capacity(monkeypatch):
    """Whether this machine's AI capacity is open, and the lanes asked about it."""
    held: dict = {"open": True, "asked": []}

    def capacity_open(lane: str) -> bool:
        held["asked"].append(lane)
        return held["open"]

    monkeypatch.setattr(lane_health, "capacity_open", capacity_open)
    return held


@pytest.fixture
def store(tmp_path, monkeypatch, capacity):
    monkeypatch.setattr(settings, "worker_id", lambda: "studio")
    monkeypatch.setattr(lane_health, "note_success", lambda lane: None)
    monkeypatch.setattr(lane_health, "note_failure", lambda lane, **kw: None)
    s = IssueStore(tmp_path)
    for n in (1, 2):
        _issue(s, n)
    return s


def test_a_follow_up_goes_only_to_the_host_that_holds_its_run(store):
    store.edit_issue(1).record_fix_run({"ending": "fixed", "patch": "p", "host": "laptop"})
    fix_review.queue(store, 1, "propose", by="op")
    assert issue_fix_worker.next_request(store.all_issues(), "studio") is None
    assert issue_fix_worker.next_request(store.all_issues(), "laptop") == 1


def test_any_host_takes_a_solve(store):
    fix_review.queue(store, 2, "solve", by="op")
    assert issue_fix_worker.next_request(store.all_issues(), "studio") == 2


def test_run_once_claims_carries_out_and_books_the_ending(store, monkeypatch):
    seen = {}

    def run_request(s, n, req, *, on_step):
        seen["req"] = req
        return "done", "Solved: fixed"

    monkeypatch.setattr(fix_review_runner, "run_request", run_request)
    fix_review.queue(store, 2, "solve", by="op")
    assert issue_fix_worker.run_once(store)
    assert seen["req"]["status"] == "running" and seen["req"]["host"] == "studio"
    assert not issue_fix_worker.run_once(store)


def test_run_once_reads_only_queued_requests_not_the_whole_table(store, monkeypatch):
    def all_issues(**kw):
        raise AssertionError("run_once read the whole issue table")

    monkeypatch.setattr(store, "all_issues", all_issues)
    monkeypatch.setattr(fix_review_runner, "run_request",
                        lambda s, n, req, *, on_step: ("done", "Solved: fixed"))
    assert not issue_fix_worker.run_once(store)
    fix_review.queue(store, 2, "solve", by="op")
    assert issue_fix_worker.run_once(store)

def _carried_out(monkeypatch) -> list[tuple[int, str]]:
    ran: list[tuple[int, str]] = []

    def run_request(s, n, req, *, on_step):
        ran.append((n, req["source"]))
        return "done", "Solved: fixed"

    monkeypatch.setattr(fix_review_runner, "run_request", run_request)
    return ran


def test_paused_capacity_holds_automation_s_request_and_runs_an_operator_s(
        store, capacity, monkeypatch):
    ran = _carried_out(monkeypatch)
    capacity["open"] = False
    fix_review.queue(store, 1, "solve", by="hunter", source="hunter")
    fix_review.queue(store, 2, "solve", by="op")
    assert issue_fix_worker.run_once(store)
    assert ran == [(2, "operator")] and capacity["asked"] == ["issue-fix"]
    assert store.load_issue(1).fix_request["status"] == "queued"
    assert not issue_fix_worker.run_once(store)


@pytest.mark.parametrize("source", ["hunter", "public", "reporter", "followup"])
def test_open_capacity_claims_automation_s_request(store, capacity, monkeypatch, source):
    ran = _carried_out(monkeypatch)
    store.edit_issue(1).record_fix_request({
        "action": "solve", "status": "queued", "source": source, "requested_by": source,
        "queued_at": _ago(minutes=1), "attempts": 1})
    assert issue_fix_worker.run_once(store)
    assert ran == [(1, source)] and capacity["asked"] == ["issue-fix"]


def test_an_operator_s_request_never_asks_the_capacity(store, capacity, monkeypatch):
    _carried_out(monkeypatch)
    capacity["open"] = False
    fix_review.queue(store, 2, "solve", by="op")
    assert issue_fix_worker.run_once(store)
    assert capacity["asked"] == []


def test_paused_capacity_still_takes_automation_s_proposal(store, capacity):
    store.edit_issue(1).record_fix_run({"ending": "fixed", "patch": "p", "host": "studio"})
    fix_review.queue(store, 1, "propose", by="public", source="public")
    capacity["open"] = False
    assert issue_fix_worker.next_request(store.all_issues(), "studio") == 1
    assert capacity["asked"] == []


def _ask_reporter(store: IssueStore, n: int, question: dict | None) -> None:
    store.edit_issue(n).record_fix_run({"ending": "fix-disputed", "host": "studio",
                                        "question": question})
    store.edit_issue(n).record_fix_request({
        "action": "ask-reporter", "status": "queued", "source": "public",
        "requested_by": "public", "queued_at": _ago(minutes=1), "attempts": 1})


def test_paused_capacity_still_takes_automation_s_drafted_question(store, capacity):
    _ask_reporter(store, 1, QUESTION)
    capacity["open"] = False
    assert issue_fix_worker.next_request(store.all_issues(), "studio") == 1
    assert capacity["asked"] == []


def test_paused_capacity_holds_automation_s_undrafted_question(store, capacity):
    _ask_reporter(store, 1, None)
    capacity["open"] = False
    assert issue_fix_worker.next_request(store.all_issues(), "studio") is None
    assert capacity["asked"] == ["issue-fix"]


def _asked(store: IssueStore, at: str) -> None:
    store.edit_issue(1).record_fix_run({
        "ending": "fix-disputed", "host": "studio",
        "question": {**QUESTION, "asked": {"url": "u", "at": at}}})


def test_a_reporter_s_reply_queues_the_answer(store, monkeypatch):
    _asked(store, datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(dispute_question, "read_answer",
                        lambda n, **kw: {"label": "B", "login": "reporter", "url": "c"})
    assert issue_fix_worker.poll_replies(store) == 1
    req = store.load_issue(1).fix_request
    assert req["action"] == "answer" and req["answer"] == {"label": "B"}
    assert req["source"] == "reporter" and req["requested_by"] == "reporter"


def test_no_reply_waits_until_the_default_is_due(store, monkeypatch):
    monkeypatch.setattr(dispute_question, "read_answer", lambda n, **kw: None)
    _asked(store, datetime.now(timezone.utc).isoformat())
    assert issue_fix_worker.poll_replies(store) == 0
    _asked(store, (datetime.now(timezone.utc) - timedelta(days=8)).isoformat())
    assert issue_fix_worker.poll_replies(store) == 1
    assert store.load_issue(1).fix_request["answer"] == {"label": "A"}


def test_an_answer_that_keeps_failing_is_not_queued_again(store, monkeypatch):
    _asked(store, datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(dispute_question, "read_answer",
                        lambda n, **kw: {"label": "B", "login": "reporter", "url": "c"})
    for _ in range(issue_fix_worker.HUNT_MAX_ATTEMPTS):
        assert issue_fix_worker.poll_replies(store) == 1
        claimed = store.claim_fix_request(1, host="studio")
        fix_review_runner._finish(store, 1, claimed, "failed", "the sandbox faulted")
    assert issue_fix_worker.poll_replies(store) == 0


def test_an_orphan_requeued_twice_ends_failed(store, monkeypatch):
    monkeypatch.setattr(issue_fix_worker.data, "store",
                        lambda: type("St", (), {"load_issue_fix_worker": lambda self: {}})())
    monkeypatch.setattr(issue_fix_worker, "STARTED_AT", "9999")
    fix_review.queue(store, 2, "solve", by="op")
    for _ in range(issue_fix_worker.MAX_REQUEUES):
        assert store.claim_fix_request(2, host="studio") is not None
        assert issue_fix_worker.recover_orphans(store) == [2]
    assert store.claim_fix_request(2, host="studio") is not None
    assert issue_fix_worker.recover_orphans(store) == []
    req = store.load_issue(2).fix_request
    assert req["status"] == "failed"
    assert issue_fix_worker.next_request(store.all_issues(), "studio") is None


def test_run_request_ends_an_unexpected_error_failed(store, monkeypatch):
    fix_review.queue(store, 2, "solve", by="op")
    claimed = store.claim_fix_request(2, host="studio")
    monkeypatch.setattr(fix_review_runner, "_carry_out",
                        lambda *a, **kw: (_ for _ in ()).throw(KeyError("result")))
    monkeypatch.setattr(fix_review_runner.issue_display, "write_story", lambda *a, **kw: None)
    status, _ = fix_review_runner.run_request(store, 2, claimed)
    assert status == "failed" and store.load_issue(2).fix_request["status"] == "failed"


BAR = fixability.BAR


def _triaged(store: IssueStore, n: int, verdict: str = "go", fixability: int = 50,
             **extra: object) -> None:
    store.edit_issue(n).record_fix_triage({"verdict": verdict, "kind": "defect",
                                           "fixability": fixability, "bar": BAR,
                                           **extra})


@pytest.fixture
def hunting(store, monkeypatch):
    monkeypatch.setattr(issue_data, "issues", lambda: store.all_issues(omit_candidates=True))
    now = datetime.now(timezone.utc)
    store.save_issue({**store.load_issue(1).raw, "meta": {
        **store.load_issue(1).raw["meta"], "created_at": (now - timedelta(days=2)).isoformat()}})
    store.save_issue({**store.load_issue(2).raw, "meta": {
        **store.load_issue(2).raw["meta"], "created_at": (now - timedelta(days=1)).isoformat()}})
    for n in (1, 2):
        _triaged(store, n)
    return store


def test_the_hunter_queues_the_newest_fresh_issue(hunting):
    assert issue_fix_worker.hunt(hunting) == 2
    req = hunting.load_issue(2).fix_request
    assert req["action"] == "solve" and req["source"] == "hunter"


def test_the_hunter_leaves_an_issue_going_with_another_pr(hunting):
    hunting.set_issue_section(2, "fix_choice", {"pr": 12, "by": "op", "at": "t", "reason": None})
    assert issue_fix_worker.hunt(hunting) == 1


def test_the_hunter_takes_only_what_the_triage_judged_go(hunting):
    now = datetime.now(timezone.utc).isoformat()
    for n, verdict in ((3, "skip"), (4, "ask")):
        _issue(hunting, n, created=now)
        _triaged(hunting, n, verdict, fixability=99)
    _issue(hunting, 5, created=now)  # not yet triaged
    _issue(hunting, 6, created=now, labels=["enhancement"])
    _triaged(hunting, 6, fixability=99)
    assert issue_fix_worker.hunt(hunting) == 2


def test_the_hunter_takes_the_most_fixable_before_the_newest(hunting):
    _triaged(hunting, 1, fixability=90)
    assert issue_fix_worker.hunt(hunting) == 1


def test_a_triage_an_edit_outdated_is_not_hunted(hunting):
    raw = hunting.load_issue(2).raw
    hunting.save_issue({**raw, "meta": {**raw["meta"], "updated_at": "2026-10-20T00:00:00Z"}})
    assert issue_fix_worker.hunt(hunting) == 1


def test_the_hunter_carries_the_replies_the_triage_read(hunting):
    _triaged(hunting, 2, guidance="maint: use option B", notes="author: it is option B")
    assert issue_fix_worker.hunt(hunting) == 2
    req = hunting.load_issue(2).fix_request
    assert req["guidance"] == "maint: use option B" and req["notes"] == "author: it is option B"


def test_the_hunter_hands_the_triage_s_approach_to_the_fix(hunting):
    _triaged(hunting, 2, notes="author: it is option B", approach="The cause is in x.ts")
    assert issue_fix_worker.hunt(hunting) == 2
    notes = hunting.load_issue(2).fix_request["notes"]
    assert "author: it is option B" in notes and "The cause is in x.ts" in notes


def test_the_hunter_keeps_to_its_daily_budget(hunting, monkeypatch):
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_HUNT_BUDGET", "1")
    assert issue_fix_worker.hunt(hunting) == 2
    assert issue_fix_worker.hunt(hunting) is None


def test_a_fill_queues_one_per_idle_slot(hunting):
    assert issue_fix_worker.fill(hunting, 3) == [2, 1]


def test_a_fill_keeps_to_the_daily_budget_across_calls(hunting, monkeypatch):
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_HUNT_BUDGET", "1")
    stale = issue_data.issues()
    monkeypatch.setattr(issue_data, "issues", lambda: stale)
    monkeypatch.setattr(hunting, "issues_matching", lambda *a: pytest.fail("read records"))
    assert issue_fix_worker.fill(hunting, 3) == [2]
    assert issue_fix_worker.fill(hunting, 3) == []


class _OnePass(threading.Event):
    """A `stop` that ends a loop after its first wait, recording each wait."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout or 0.0)
        self.set()
        return True


@pytest.fixture
def one_pass(monkeypatch) -> _OnePass:
    one = _OnePass()
    monkeypatch.setattr(issue_fix_worker, "stop", one)
    monkeypatch.setattr(issue_fix_worker.schema_gate, "paused", lambda lane: False)
    monkeypatch.setattr(issue_fix_worker.profile_gate, "paused", lambda lane: False)
    return one


@pytest.mark.parametrize("budget, triaged", [("1", False), ("2", True)])
def test_a_spent_budget_skips_the_hunt(hunting, monkeypatch, one_pass, budget, triaged):
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_HUNT", "1")
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_HUNT_BUDGET", budget)
    assert issue_fix_worker.hunt(hunting) == 2
    calls: list[str] = []
    monkeypatch.setattr(issue_fix_worker, "IssueStore", lambda: hunting)
    monkeypatch.setattr(issue_fix_worker, "_reclaim", lambda s: None)
    monkeypatch.setattr(issue_fix_worker, "poll_replies", lambda s: 0)
    monkeypatch.setattr(issue_fix_worker, "_every_ten_minutes", lambda s: None)
    monkeypatch.setattr(lane_health, "open_or_retest", lambda lane: True)
    monkeypatch.setattr(issue_fix_worker, "triage", lambda s, n: calls.append("triage") or [])
    issue_fix_worker._drain(3)
    assert (calls == ["triage"]) is triaged


def test_idle_slots_are_those_no_solve_fills(store, monkeypatch):
    assert issue_fix_worker._idle(store, 3) == 3
    fix_review.queue(store, 1, "solve", by="op")
    monkeypatch.setattr(issue_fix_worker, "in_flight", lambda: [7])
    assert issue_fix_worker._idle(store, 3) == 1
    store.edit_issue(2).record_fix_run({"ending": "fixed", "patch": "p", "host": "studio"})
    fix_review.queue(store, 2, "propose", by="op")
    assert issue_fix_worker._idle(store, 3) == 1
    assert issue_fix_worker._idle(store, 2) == 0
    assert issue_fix_worker._idle(store, 1) == -1


def test_no_idle_slot_hunts_nothing(hunting, monkeypatch, one_pass):
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_HUNT", "1")
    calls: list[str] = []
    monkeypatch.setattr(issue_fix_worker, "IssueStore", lambda: hunting)
    monkeypatch.setattr(issue_fix_worker, "_reclaim", lambda s: None)
    monkeypatch.setattr(issue_fix_worker, "poll_replies", lambda s: 0)
    monkeypatch.setattr(issue_fix_worker, "_every_ten_minutes", lambda s: None)
    monkeypatch.setattr(lane_health, "open_or_retest", lambda lane: True)
    monkeypatch.setattr(issue_fix_worker, "_idle", lambda s, slots: 0)
    monkeypatch.setattr(issue_fix_worker, "triage", lambda s, n: calls.append("triage") or [])
    monkeypatch.setattr(issue_fix_worker, "fill", lambda s, want: calls.append("fill") or [])
    issue_fix_worker._drain(3)
    assert calls == []


@pytest.mark.parametrize("setting, screened", [("1", True), ("0", False)])
def test_the_feature_screen_setting_alone_runs_the_screener(hunting, monkeypatch, one_pass,
                                                             setting, screened):
    monkeypatch.setenv("TRIAGE_FEATURE_SCREEN", setting)
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_HUNT", "0")
    calls: list[str] = []
    monkeypatch.setattr(issue_fix_worker, "IssueStore", lambda: hunting)
    monkeypatch.setattr(issue_fix_worker, "_reclaim", lambda s: None)
    monkeypatch.setattr(issue_fix_worker, "poll_replies", lambda s: 0)
    monkeypatch.setattr(issue_fix_worker, "_every_ten_minutes", lambda s: None)
    monkeypatch.setattr(lane_health, "open_or_retest", lambda lane: True)
    monkeypatch.setattr(issue_fix_worker, "_idle", lambda s, slots: 0)
    monkeypatch.setattr(issue_fix_worker, "screen_features",
                        lambda s, n: calls.append("screen") or [])
    issue_fix_worker._drain(3)
    assert (calls == ["screen"]) is screened


def test_a_fill_stops_when_capacity_closes_midway(hunting, capacity, monkeypatch):
    queue = fix_review.queue

    def closing(*args, **kwargs):
        result = queue(*args, **kwargs)
        capacity["open"] = False
        return result

    monkeypatch.setattr(fix_review, "queue", closing)
    assert issue_fix_worker.fill(hunting, 3) == [2]


def test_the_hunter_queues_nothing_while_capacity_is_paused(hunting, capacity):
    capacity["open"] = False
    assert issue_fix_worker.hunt(hunting) is None
    assert capacity["asked"] == ["issue-fix"]
    assert all(i.fix_request is None for i in hunting.all_issues().values())


def test_the_hunter_with_no_pick_never_asks_the_capacity(hunting, capacity, monkeypatch):
    monkeypatch.setattr(issue_data, "issues", lambda: {})
    assert issue_fix_worker.hunt(hunting) is None
    assert capacity["asked"] == []


def test_the_hunter_takes_a_maintainer_s_issue_ahead_of_a_newer_one(hunting):
    raw = hunting.load_issue(1).raw
    hunting.save_issue({**raw, "meta": {**raw["meta"], "author_association": "MEMBER"}})
    assert issue_fix_worker.hunt(hunting) == 1


def test_the_hunter_takes_a_maintainer_s_issue_ahead_of_a_more_fixable_one(hunting):
    _triaged(hunting, 2, fixability=95)
    raw = hunting.load_issue(1).raw
    hunting.save_issue({**raw, "meta": {**raw["meta"], "author_association": "MEMBER"}})
    assert issue_fix_worker.hunt(hunting) == 1


@pytest.fixture
def triaging(hunting, monkeypatch, tmp_path):
    """The hunter's issues with no triage yet, a pinned base, and a triage
    agent that answers from `judged["verdict"]`."""
    for n in (1, 2):
        hunting.save_issue({k: v for k, v in hunting.load_issue(n).raw.items()
                            if k != "fix_triage"})
    monkeypatch.setattr(prove, "pinned", lambda st: prove.PinnedBase(
        sha="b" * 40, tier=2, image="img", clone=tmp_path))
    monkeypatch.setattr(public_loop, "read_replies", lambda issue, after: [])
    monkeypatch.setattr(issue_fix_worker, "_triage_failed", {})
    judged: dict = {"verdict": {"verdict": "go", "kind": "defect", "fixability": 70, "bar": BAR},
                    "issues": [], "asked": []}

    def judge(issue, clone, *, base_sha, replies=(), subject=None):
        judged["issues"].append(issue.number)
        if isinstance(judged["verdict"], Exception):
            raise judged["verdict"]
        return dict(judged["verdict"])

    monkeypatch.setattr(fixability, "judge", judge)
    monkeypatch.setattr(public_loop, "ask_triage",
                        lambda issue, record: judged["asked"].append(issue.number) or "posted")
    return judged


def test_the_triage_judges_the_newest_untriaged_issue_and_books_it(hunting, triaging):
    assert issue_fix_worker.triage(hunting, 1) == [2]
    assert hunting.load_issue(2).fix_triage["verdict"] == "go"
    assert issue_fix_worker.triage(hunting, 1) == [1]
    assert issue_fix_worker.triage(hunting, 1) == []
    phases = [r.raw.get("phase") for r in hunting.runs() if hasattr(r, "raw")]
    assert phases.count("issue-triage:run") == 2


def test_a_batch_judges_each_issue_once_newest_first(hunting, triaging):
    assert issue_fix_worker.triage(hunting, 5) == [2, 1]
    assert triaging["issues"] == [2, 1]


def test_a_batch_stops_when_capacity_closes(hunting, triaging, capacity, monkeypatch):
    judge = fixability.judge

    def judge_then_close(*a, **kw):
        capacity["open"] = False
        return judge(*a, **kw)

    monkeypatch.setattr(fixability, "judge", judge_then_close)
    assert issue_fix_worker.triage(hunting, 5) == [2]


def test_an_ask_posts_its_question_and_counts_it(hunting, triaging):
    triaging["verdict"] = {"verdict": "ask", "kind": "defect", "question": "A or B?"}
    issue_fix_worker.triage(hunting, 1)
    tri = hunting.load_issue(2).fix_triage
    assert triaging["asked"] == [2] and tri["asked"] == "posted" and tri["asks"] == 1


def test_a_triage_that_cannot_finish_rests_the_issue(hunting, triaging):
    triaging["verdict"] = ValueError("no JSON object found in agent output")
    assert issue_fix_worker.triage(hunting, 1) == []
    assert issue_fix_worker.triage(hunting, 1) == []
    assert triaging["issues"] == [2, 1]  # 2 rests; 1 is tried next


def test_a_triage_that_keeps_failing_is_skipped_until_the_report_changes(hunting, triaging):
    triaging["verdict"] = ValueError("no JSON object found in agent output")
    for _ in range(fixability.MAX_TRIAGE_FAILS):
        issue_fix_worker._triage_failed.clear()
        issue_fix_worker.triage(hunting, 1)
    assert triaging["issues"] == [2] * fixability.MAX_TRIAGE_FAILS
    assert hunting.load_issue(2).fix_triage["verdict"] == "skip"
    issue_fix_worker._triage_failed.clear()
    issue_fix_worker.triage(hunting, 1)
    assert triaging["issues"][-1] == 1  # 2 is no longer picked


def test_no_triage_runs_while_capacity_is_paused(hunting, triaging, capacity):
    capacity["open"] = False
    assert issue_fix_worker.triage(hunting, 5) == [] and triaging["issues"] == []


def test_the_backfill_judges_every_untriaged_issue(hunting, triaging, monkeypatch, capsys):
    from issue_triage import issue_store
    from pipeline import cli
    monkeypatch.setattr(issue_store, "IssueStore", lambda: hunting)
    assert cli.main(["triage-backfill"]) == 0
    assert triaging["issues"] == [2, 1] and "triaged 2 issues" in capsys.readouterr().out
    triggers = [r.raw.get("trigger") for r in hunting.runs()
                if hasattr(r, "raw") and r.raw.get("phase") == "issue-triage:run"]
    assert triggers == ["backfill", "backfill"]


@pytest.fixture
def screening(hunting, monkeypatch, tmp_path):
    """Issues 3-6 beside the hunter's defects: an old `enhancement`, a decided
    one, one screened fresh, and a defect; a pinned base, a digest and a
    screener that answers from `screened["section"]`."""
    old = "2025-01-01T00:00:00Z"
    _issue(hunting, 3, created=old, labels=["enhancement"])
    _issue(hunting, 4, created=old, labels=["enhancement"])
    _issue(hunting, 5, created=old, labels=["enhancement"])
    _issue(hunting, 6, created=old)
    hunting.edit_issue(4).record_feature_decision({"decision": "decline"})
    monkeypatch.setattr(prove, "pinned", lambda st: prove.PinnedBase(
        sha="b" * 40, tier=2, image="img", clone=tmp_path))
    monkeypatch.setattr(public_loop, "read_replies", lambda issue, after: [])
    monkeypatch.setattr(feature_history, "refresh", lambda issues, *, force=False: tmp_path)
    screened: dict = {"section": {"verdict": "consider", "size": "small", "mockup": "<html>"},
                      "issues": []}

    def screen(issue, clone, history, *, base_sha, replies, subject):
        screened["issues"].append(issue.number)
        if isinstance(screened["section"], Exception):
            raise screened["section"]
        return dict(screened["section"])

    monkeypatch.setattr(feature_screen, "screen", screen)
    hunting.edit_issue(5).record_feature_screen({"verdict": "unclear"})
    return screened


def test_screen_features_picks_unscreened_feature_requests_only(hunting, screening):
    assert issue_fix_worker.screen_features(hunting, None) == [3]
    assert screening["issues"] == [3]
    assert hunting.load_issue(3).feature_screen["verdict"] == "consider"
    assert issue_fix_worker.screen_features(hunting, None) == []


def test_nothing_to_screen_never_asks_the_capacity(hunting, screening, capacity):
    hunting.edit_issue(3).record_feature_decision({"decision": "decline"})
    assert issue_fix_worker.screen_features(hunting, None) == []
    assert capacity["asked"] == [] and screening["issues"] == []


def test_screen_features_books_ledger_phase(hunting, screening):
    issue_fix_worker.screen_features(hunting, 1, trigger="backfill")
    runs = [r.raw for r in hunting.runs() if hasattr(r, "raw")
            and r.raw.get("phase") == "feature-screen:run"]
    assert len(runs) == 1 and runs[0]["issue"] == 3 and runs[0]["trigger"] == "backfill"
    assert runs[0]["stats"] == {"verdict": "consider", "size": "small", "mockup": True}


def test_screen_features_stops_when_capacity_closes(hunting, screening, capacity, monkeypatch):
    _issue(hunting, 7, created="2025-02-01T00:00:00Z", labels=["enhancement"])
    section = feature_screen.screen

    def screen_then_close(*a, **kw):
        capacity["open"] = False
        return section(*a, **kw)

    monkeypatch.setattr(feature_screen, "screen", screen_then_close)
    assert issue_fix_worker.screen_features(hunting, None) == [7]


def _age_screen(store: IssueStore, n: int, seconds: float) -> None:
    rec = store.load_issue(n).raw
    rec["feature_screen"]["checked_at"] = _ago(seconds=seconds)
    store.save_issue(rec)


def test_a_failed_screen_is_recorded_and_rests_until_the_retry_time(hunting, screening):
    screening["section"] = ValueError("no JSON object found in agent output")
    assert issue_fix_worker.screen_features(hunting, None) == []
    screen = hunting.load_issue(3).feature_screen
    assert screen["verdict"] == "failed" and "no JSON object" in screen["error"]
    assert feature_screen.current(hunting.load_issue(3)) is None
    assert issue_fix_worker.screen_features(hunting, None) == []
    assert screening["issues"] == [3]
    _age_screen(hunting, 3, issue_fix_worker.TRIAGE_RETRY_SECONDS + 60)
    screening["section"] = {"verdict": "unclear"}
    assert issue_fix_worker.screen_features(hunting, None) == [3]
    assert screening["issues"] == [3, 3]


def test_an_issue_another_worker_claimed_is_skipped(hunting, screening):
    hunting.edit_issue(3).record_feature_screen({"verdict": "screening", "by": "studio"})
    assert issue_fix_worker.screen_features(hunting, None) == []
    assert screening["issues"] == []
    assert feature_screen.current(hunting.load_issue(3)) is None


def test_an_abandoned_claim_is_retaken(hunting, screening):
    hunting.edit_issue(3).record_feature_screen({"verdict": "screening", "by": "studio"})
    _age_screen(hunting, 3, issue_fix_worker.SCREEN_CLAIM_SECONDS + 60)
    assert issue_fix_worker.screen_features(hunting, None) == [3]
    assert hunting.load_issue(3).feature_screen["verdict"] == "consider"


def test_a_screen_holds_its_claim_while_the_agent_runs(hunting, screening, monkeypatch):
    seen = []

    def screen(issue, *a, **kw):
        seen.append(hunting.load_issue(issue.number).feature_screen)
        return {"verdict": "unclear"}

    monkeypatch.setattr(feature_screen, "screen", screen)
    monkeypatch.setattr(settings, "worker_id", lambda: "studio")
    assert issue_fix_worker.screen_features(hunting, None) == [3]
    assert seen[0]["verdict"] == "screening" and seen[0]["by"] == "studio"


def test_an_agent_outage_releases_the_claim(hunting, screening):
    screening["section"] = headless_agent.AgentUnavailable("down")
    with pytest.raises(headless_agent.AgentUnavailable):
        issue_fix_worker.screen_features(hunting, None)
    assert hunting.load_issue(3).feature_screen is None


def test_the_feature_screen_backfill_prints_yes_rate(hunting, screening, monkeypatch, capsys):
    from issue_triage import issue_store
    from pipeline import cli
    _issue(hunting, 7, created="2025-02-01T00:00:00Z", labels=["enhancement"])
    monkeypatch.setattr(issue_store, "IssueStore", lambda: hunting)
    answers = iter([{"verdict": "consider"}, {"verdict": "not-a-fit"}])
    monkeypatch.setattr(feature_screen, "screen", lambda *a, **kw: next(answers))
    assert cli.main(["feature-screen-backfill"]) == 0
    assert "screened 2, consider 1 (50%)" in capsys.readouterr().out


def test_a_triage_write_keeps_a_request_claimed_meanwhile(store):
    handle = store.edit_issue(1)
    fix_review.queue(store, 1, "solve", by="op")
    assert store.claim_fix_request(1, host="studio") is not None
    handle.record_fix_triage({"verdict": "go", "kind": "defect", "fixability": 70})
    issue = store.load_issue(1)
    assert issue.fix_request["status"] == "running" and issue.fix_triage["verdict"] == "go"


def test_a_priority_author_s_queued_request_runs_first(store, monkeypatch):
    fix_review.queue(store, 1, "solve", by="op")
    fix_review.queue(store, 2, "solve", by="op")
    raw = store.load_issue(2).raw
    store.save_issue({**raw, "meta": {**raw["meta"], "author": "eager-dev"}})
    assert issue_fix_worker.next_request(store.all_issues(), "studio") == 1
    monkeypatch.setattr(profile, "active",
                        lambda: profile.RepoProfile(priority_authors=("eager-dev",)))
    assert issue_fix_worker.next_request(store.all_issues(), "studio") == 2


def _ago(**kw: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def _failed(store: IssueStore, n: int, *, finished: str, attempts: int = 1,
            guidance: str | None = None, notes: str | None = None) -> None:
    req = {"action": "solve", "status": "failed", "source": "hunter", "requested_by": "hunter",
           "queued_at": finished, "started_at": finished, "finished_at": finished,
           "attempts": attempts, "reason": "interrupted: the issue-fix worker restarted"}
    if guidance:
        req["guidance"] = guidance
    if notes:
        req["notes"] = notes
    store.edit_issue(n).record_fix_request(req)


def test_the_hunter_rests_a_failed_solve_before_retrying_it(hunting):
    _failed(hunting, 2, finished=_ago(minutes=5))
    assert issue_fix_worker.hunt(hunting) == 1


def test_the_hunter_retries_a_failed_solve_once_it_has_rested(hunting):
    _failed(hunting, 2, finished=_ago(hours=2))
    assert issue_fix_worker.hunt(hunting) == 2
    req = hunting.load_issue(2).fix_request
    assert req["status"] == "queued" and req["source"] == "hunter" and req["attempts"] == 2


def test_the_hunter_stops_retrying_after_its_attempts(hunting):
    _failed(hunting, 2, finished=_ago(hours=2), attempts=issue_fix_worker.HUNT_MAX_ATTEMPTS)
    assert issue_fix_worker.hunt(hunting) == 1


@pytest.mark.parametrize("words", [{"guidance": "look at x"}, {"notes": "reporter: > x"}])
def test_the_hunter_leaves_a_failed_solve_with_words_to_carry_alone(hunting, words):
    _failed(hunting, 2, finished=_ago(hours=2), **words)
    assert issue_fix_worker.hunt(hunting) == 1


@pytest.fixture
def beats(store, tmp_path, monkeypatch):
    """The shared store the lane's heartbeats live in."""
    st = S.Store(tmp_path / "prs")
    monkeypatch.setattr(data, "_store", st)
    return st


def _claimed(store: IssueStore, n: int, host: str, *, at: str | None = None) -> None:
    fix_review.queue(store, n, "solve", by="op")
    store.claim_fix_request(n, host=host)
    if at:
        issue = store.edit_issue(n)
        issue.record_fix_request({**issue.fix_request, "started_at": at})


def test_the_heartbeat_names_every_issue_in_flight(store, beats, monkeypatch):
    monkeypatch.setitem(issue_fix_worker.state, "current", {4, 2})
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_HUNT", "1")
    issue_fix_worker.beat()
    rec = beats.load_issue_fix_worker()["hosts"]["studio"]
    assert rec["current_issue"] == 2 and rec["current_issues"] == [2, 4]
    assert rec["last_beat"] and rec["autohunt"] is True


def test_an_idle_heartbeat_names_no_issue(store, beats):
    issue_fix_worker.beat()
    rec = beats.load_issue_fix_worker()["hosts"]["studio"]
    assert rec["current_issue"] is None and rec["current_issues"] == []


def test_racing_claims_on_one_request_land_once(store):
    fix_review.queue(store, 2, "solve", by="op")
    barrier = threading.Barrier(8)
    got: dict[str, dict | None] = {}

    def claim(host: str) -> None:
        barrier.wait()
        got[host] = store.claim_fix_request(2, host=host)

    threads = [threading.Thread(target=claim, args=(f"s{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    [winner] = [h for h, c in got.items() if c is not None]
    assert len(got) == 8 and store.load_issue(2).fix_request["host"] == winner


@pytest.fixture
def slots(monkeypatch):
    """Slot threads to start, joined with `stop` set however the test ends."""
    monkeypatch.setattr(issue_fix_worker, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(issue_fix_worker, "BUSY_POLL_SECONDS", 0.01)
    monkeypatch.setattr(lane_health, "is_open", lambda lane: True)
    monkeypatch.setattr(issue_fix_worker.schema_gate, "paused", lambda lane: False)
    monkeypatch.setattr(issue_fix_worker.profile_gate, "paused", lambda lane: False)
    started: list[threading.Thread] = []

    def start(target, count: int = 1) -> list[threading.Thread]:
        new = [threading.Thread(target=target, daemon=True) for _ in range(count)]
        started.extend(new)
        for t in new:
            t.start()
        return new

    issue_fix_worker.stop.clear()
    try:
        yield start
    finally:
        issue_fix_worker.stop.set()
        for t in started:
            t.join(5)
        issue_fix_worker.stop.clear()
        issue_fix_worker._drained.clear()


def _until(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def test_slots_run_every_request_once_and_no_issue_twice(store, monkeypatch, slots):
    for n in (3, 4, 5, 6):
        _issue(store, n)
    for n in range(1, 7):
        fix_review.queue(store, n, "solve", by="op")
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_SLOTS", "3")
    ran: list[int] = []
    together: list[list[int]] = []
    # The first two requests each wait for the other, so two slots run at once.
    overlap = threading.Barrier(
        2, action=lambda: together.append(issue_fix_worker.in_flight()), timeout=5)
    lock = threading.Lock()

    def run_request(s, n, req, *, on_step):
        with lock:
            ran.append(n)
            first_two = len(ran) <= 2
        if first_two:
            overlap.wait()
        return "done", "ok"

    monkeypatch.setattr(fix_review_runner, "run_request", run_request)
    monkeypatch.setattr(issue_fix_worker, "IssueStore", lambda: store)
    workers = slots(issue_fix_worker._slot_loop, 3)
    assert _until(lambda: len(ran) == 6 and not issue_fix_worker.in_flight())
    issue_fix_worker.stop.set()
    for t in workers:
        t.join(5)
    assert sorted(ran) == [1, 2, 3, 4, 5, 6]
    assert len(together) == 1 and len(together[0]) == 2
    assert all(store.load_issue(n).fix_request["host"] == "studio" for n in range(1, 7))


def test_a_slot_never_retests_a_tripped_lane(store, monkeypatch, one_pass):
    rec = {"lanes": {"issue-fix": {"tripped": {"at": "2020-01-01T00:00:00+00:00",
                                               "kind": "sandbox", "reason": "r"}}}}
    retests: list[str] = []
    monkeypatch.setattr(lane_health.worker_health, "load", lambda st, host: rec)
    monkeypatch.setattr(lane_health.lane_pause, "paused", lambda host, lane: False)
    monkeypatch.setattr(lane_health.worker_selftest, "run", lambda kind: retests.append(kind) or "down")
    monkeypatch.setattr(issue_fix_worker, "run_once", lambda s: pytest.fail("picked"))
    monkeypatch.setattr(issue_fix_worker, "IssueStore", lambda: store)
    issue_fix_worker._slot_loop()
    assert retests == []
    monkeypatch.setattr(lane_health, "note_account", lambda: None)
    monkeypatch.setattr(lane_health, "_update", lambda fn: {})
    assert not lane_health.open_or_retest("issue-fix") and retests == ["sandbox"]


def test_a_lost_claim_looks_again_at_once(store, monkeypatch, one_pass):
    fix_review.queue(store, 2, "solve", by="op")
    monkeypatch.setattr(store, "claim_fix_request", lambda n, **kw: None)
    monkeypatch.setattr(lane_health, "is_open", lambda lane: True)
    monkeypatch.setattr(issue_fix_worker, "IssueStore", lambda: store)
    assert issue_fix_worker.run_once(store)
    issue_fix_worker._slot_loop()
    assert one_pass.waits == [issue_fix_worker.BUSY_POLL_SECONDS]


def test_the_heartbeat_outlives_a_slot_still_running(store, beats, monkeypatch, slots):
    fix_review.queue(store, 2, "solve", by="op")
    entered, release = threading.Event(), threading.Event()

    def run_request(s, n, req, *, on_step):
        entered.set()
        release.wait(5)
        return "done", "ok"

    monkeypatch.setattr(fix_review_runner, "run_request", run_request)
    monkeypatch.setattr(issue_fix_worker, "IssueStore", lambda: store)
    monkeypatch.setattr(issue_fix_worker, "_drain", lambda slots: None)
    monkeypatch.setattr(issue_fix_worker, "_threads", [])
    monkeypatch.setattr(issue_fix_worker.worker_log, "install", lambda: None)
    monkeypatch.setattr(issue_fix_worker.verify_driver, "large_slots", lambda: 8)
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_WORKER", "1")
    try:
        assert issue_fix_worker.startup()
        assert entered.wait(5)
        issue_fix_worker.stop.set()
        assert not issue_fix_worker._drained.wait(0.2)
        assert "studio" in beats.load_issue_fix_worker()["hosts"]
        release.set()
        assert issue_fix_worker._drained.wait(5)
        assert issue_fix_worker.shutdown(5)
        assert "studio" not in beats.load_issue_fix_worker()["hosts"]
    finally:
        release.set()
        issue_fix_worker.shutdown(5)


def test_more_slots_than_large_sandbox_slots_warns(monkeypatch, capsys):
    monkeypatch.setattr(issue_fix_worker.verify_driver, "large_slots", lambda: 2)
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_SLOTS", "4")
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_WORKER", "1")
    monkeypatch.setattr(issue_fix_worker, "_threads", [])
    monkeypatch.setattr(issue_fix_worker.worker_log, "install", lambda: None)
    for name in ("_drain", "_slot_loop", "_beat_loop"):
        monkeypatch.setattr(issue_fix_worker, name, lambda *a: None)
    try:
        assert issue_fix_worker.startup()
    finally:
        issue_fix_worker.shutdown(5)
        issue_fix_worker.stop.clear()
        issue_fix_worker._drained.clear()
    warnings = [line for line in capsys.readouterr().out.splitlines()
                if line.startswith("[issue-fix] warning:")]
    assert len(warnings) == 1 and "4" in warnings[0] and "2" in warnings[0]


def test_a_lane_that_has_drained_drops_its_heartbeat(store, beats, monkeypatch):
    issue_fix_worker.beat()
    monkeypatch.setattr(issue_fix_worker, "_drained", threading.Event())
    issue_fix_worker._beat_loop([])
    assert issue_fix_worker._drained.is_set()
    assert "studio" not in beats.load_issue_fix_worker()["hosts"]


def test_a_claim_this_host_made_before_the_process_started_is_requeued(store, beats, monkeypatch):
    beats.save_issue_fix_worker({"host": "studio", "last_beat": _ago()})
    _claimed(store, 1, "studio", at=_ago(minutes=5))
    monkeypatch.setattr(issue_fix_worker, "STARTED_AT", _ago(minutes=1))
    assert issue_fix_worker.recover_orphans(store) == [1]
    issue = store.load_issue(1)
    req = issue.fix_request
    assert req["status"] == "queued" and req["attempts"] == 1
    assert not {"host", "started_at", "finished_at", "reason"} & req.keys()
    note = issue.fix_thread[-1]
    assert note["by"] == "worker" and note["text"].startswith("requeued:")
    assert "restarted" in note["text"]


def test_a_restart_requeues_every_slot_s_claim_without_spending_an_attempt(
        store, beats, monkeypatch):
    for n in (1, 2):
        _claimed(store, n, "studio", at=_ago(minutes=5))
    issue = store.edit_issue(2)
    issue.record_fix_request({**issue.fix_request, "attempts": 2})
    monkeypatch.setattr(issue_fix_worker, "STARTED_AT", _ago(minutes=1))
    assert issue_fix_worker.recover_orphans(store) == [1, 2]
    assert [store.load_issue(n).fix_request["status"] for n in (1, 2)] == ["queued"] * 2
    assert store.load_issue(2).fix_request["attempts"] == 2
    assert issue_fix_worker.run_once(store)


def test_a_claim_this_process_made_is_left_running(store, beats, monkeypatch):
    monkeypatch.setattr(issue_fix_worker, "STARTED_AT", _ago(minutes=1))
    _claimed(store, 1, "studio")
    assert issue_fix_worker.recover_orphans(store) == []
    assert store.load_issue(1).fix_request["status"] == "running"


@pytest.mark.parametrize("beat,claimed,want", [
    (_ago(hours=2), _ago(hours=3), [1]),
    (_ago(minutes=1), _ago(hours=3), []),
    (None, _ago(hours=2), [1]),
    (None, _ago(minutes=5), []),
], ids=["silent", "live", "never-beat-old-claim", "never-beat-fresh-claim"])
def test_another_host_s_claim_is_requeued_once_its_heartbeat_is_silent(
        store, beats, beat, claimed, want):
    if beat:
        beats.save_issue_fix_worker({"host": "laptop", "last_beat": beat})
    _claimed(store, 1, "laptop", at=claimed)
    assert issue_fix_worker.recover_orphans(store) == want
    req = store.load_issue(1).fix_request
    if want:
        assert req["status"] == "queued" and "host" not in req
        assert "laptop went offline" in store.load_issue(1).fix_thread[-1]["text"]
    else:
        assert req["status"] == "running"


def test_recovery_leaves_a_request_that_ended_since_it_was_read(store, beats, monkeypatch):
    _claimed(store, 1, "laptop", at=_ago(hours=3))
    real = store.issues_matching

    def read_then_finish(path, values):
        found = real(path, values)
        issue = store.edit_issue(1)
        issue.record_fix_request({**issue.fix_request, "status": "done"})
        return found

    monkeypatch.setattr(store, "issues_matching", read_then_finish)
    assert issue_fix_worker.recover_orphans(store) == []
    assert store.load_issue(1).fix_request["status"] == "done"



def test_an_agent_outage_in_the_ten_minute_pass_trips_the_lanes(store, monkeypatch):
    from issue_triage import followup, public_loop
    from pipeline import headless_agent

    tripped, ran = [], []

    def down(store_, **_):
        raise headless_agent.AgentUnavailable("not logged in")

    monkeypatch.setattr(followup, "poll", down)
    monkeypatch.setattr(public_loop, "refresh", lambda store_: ran.append("refresh"))
    monkeypatch.setattr(lane_health, "trip_agent_lanes", lambda reason: tripped.append(reason))
    issue_fix_worker._every_ten_minutes(store)
    assert tripped == ["not logged in"] and ran == []


def _ten_minute_steps(monkeypatch) -> list[str]:
    from issue_triage import followup, public_loop

    ran: list[str] = []
    for module, name in ((followup, "poll"), (public_loop, "refresh"),
                         (public_loop, "answer_replies"), (public_loop, "sync")):
        monkeypatch.setattr(module, name, lambda store_, name=name, **_: ran.append(name))
    return ran


def test_the_ten_minute_pass_follows_up_before_it_reads_replies_or_writes(store,
                                                                         monkeypatch):
    ran = _ten_minute_steps(monkeypatch)
    issue_fix_worker._every_ten_minutes(store)
    assert ran == ["poll", "refresh", "answer_replies", "sync"]


def test_reply_routing_asks_the_capacity_only_when_a_reply_needs_it(store, capacity,
                                                                     monkeypatch):
    from issue_triage import followup, public_loop
    handed: dict = {}
    monkeypatch.setattr(followup, "poll",
                        lambda store_, **kw: handed.setdefault("poll", kw["may_route"]))
    monkeypatch.setattr(public_loop, "refresh", lambda store_, **kw: None)
    monkeypatch.setattr(public_loop, "answer_replies",
                        lambda store_, **kw: handed.setdefault("answer", kw["may_route"]))
    monkeypatch.setattr(public_loop, "sync", lambda store_, **kw: None)
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_PUBLIC", "live")
    capacity["open"] = False
    issue_fix_worker._every_ten_minutes(store)
    assert capacity["asked"] == []
    assert handed["answer"]() is False and handed["poll"]() is False
    assert capacity["asked"] == ["issue-fix", "issue-fix"]


def test_a_dry_run_public_loop_reads_replies_without_asking_the_capacity(
        store, capacity, monkeypatch):
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_PUBLIC", "dry-run")
    ran = _ten_minute_steps(monkeypatch)
    capacity["open"] = False
    issue_fix_worker._every_ten_minutes(store)
    assert "answer_replies" in ran and capacity["asked"] == []


def test_automation_s_request_is_metered_and_an_operator_s_is_not(store, capacity, monkeypatch):
    from pipeline import capacity as policy
    lanes: list[tuple[str, str | None]] = []

    def run_request(s, n, req, *, on_step):
        lanes.append((req["source"], policy.meter_lane()))
        return "done", "Solved: fixed"
    monkeypatch.setattr(fix_review_runner, "run_request", run_request)
    fix_review.queue(store, 1, "solve", by="op")
    issue_fix_worker.run_once(store)
    store.edit_issue(2).record_fix_request({
        "action": "solve", "status": "queued", "source": "hunter", "requested_by": "hunter",
        "queued_at": _ago(minutes=1), "attempts": 1})
    issue_fix_worker.run_once(store)
    assert lanes == [("operator", None), ("hunter", "issue-fix")]


@pytest.mark.parametrize("question, lane", [(QUESTION, None), (None, "issue-fix")])
def test_only_an_undrafted_question_is_metered(store, monkeypatch, question, lane):
    from pipeline import capacity as policy
    lanes: list[str | None] = []

    def run_request(s, n, req, *, on_step):
        lanes.append(policy.meter_lane())
        return "done", "Asked"
    monkeypatch.setattr(fix_review_runner, "run_request", run_request)
    _ask_reporter(store, 1, question)
    assert issue_fix_worker.run_once(store)
    assert lanes == [lane]
