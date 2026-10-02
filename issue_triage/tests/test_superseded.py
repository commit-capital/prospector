"""Stepping an issue's fix attempt aside for someone else's pull request: what
counts as a rival, the mark a check records or clears, and the sweep over the
attempts waiting on their next step."""
from __future__ import annotations

import pytest

from issue_triage import fix_review, related_prs, superseded
from issue_triage.issue_store import IssueStore
from pipeline import settings

QUESTION = {"question": "2 or 3?", "options": [{"label": "A", "behavior": "2"},
                                               {"label": "B", "behavior": "3"}],
            "default": "A", "default_reason": "r", "asked": {"url": "u", "at": "t"}}


def _pr(number: int, *, author: str | None = "contrib", state: str = "open",
        closes: bool = False) -> dict:
    return {"number": number, "title": f"fix {number}", "state": state, "author": author,
            "closes": closes}


@pytest.fixture
def found(monkeypatch):
    """What the search for pull requests naming an issue answers, and the issues
    it was asked about."""
    held: dict = {"prs": [], "asked": []}

    def search(issue, exclude=None):
        held["asked"].append(issue)
        if held["prs"] is None:
            return None
        return [p for p in held["prs"] if p["number"] not in (exclude or set())]

    monkeypatch.setattr(related_prs, "search", search)
    return held


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "push_login", lambda: "pushbot")
    s = IssueStore(tmp_path)
    for n in (7, 8):
        _save(s, n)
    return s


def _save(store: IssueStore, n: int, *, state: str = "open") -> None:
    store.save_issue({"issue": n, "meta": {"title": f"bug {n}", "state": state, "body": "b",
                                           "updated_at": "2026-10-01T00:00:00Z",
                                           "author": "reporter"}})


def _run(store: IssueStore, n: int = 7, **over) -> None:
    store.edit_issue(n).record_fix_run({"ending": "fix-disputed", "detail": "d",
                                        "host": "studio", "question": QUESTION,
                                        "proposal": None, **over})


def test_an_open_pull_request_by_someone_else_supersedes_the_attempt(store, found):
    _run(store)
    found["prs"] = [_pr(14918)]
    mark = superseded.check(store, 7)
    assert mark is not None and (mark["pr"], mark["author"]) == (14918, "contrib")
    issue = store.load_issue(7)
    assert issue.fix_run["superseded"] == mark
    assert fix_review.fix_status(issue) == ("superseded",
                                            "#14918 by contrib is open on the issue")


def test_the_bot_s_and_the_push_user_s_and_closed_pull_requests_are_no_rivals(store, found):
    _run(store)
    found["prs"] = [_pr(1, author="test-bot[bot]"), _pr(2, author="pushbot"),
                    _pr(3, state="closed"), _pr(4, state="merged")]
    assert superseded.check(store, 7) is None
    assert "superseded" not in store.load_issue(7).fix_run


def test_an_open_proposal_of_the_attempt_s_own_is_left_to_the_follow_up(store, found):
    _run(store, ending="fixed", question=None, proposal={"pr": 9})
    found["prs"] = [_pr(14918)]
    assert superseded.check(store, 7) is None
    assert found["asked"] == []


def test_a_closed_proposal_of_the_attempt_s_own_is_no_rival(store, found):
    _run(store, ending="fixed", question=None, proposal={"pr": 9})
    store.edit_issue(7).record_fix_followup({"pr": 9, "state": "done", "closed_as": "closed"})
    found["prs"] = [_pr(9, author=None)]
    assert superseded.check(store, 7) is None
    assert found["asked"] == [7]


def test_a_search_that_does_not_answer_leaves_the_mark_as_it_was(store, found):
    _run(store)
    found["prs"] = None
    assert superseded.check(store, 7) is None
    found["prs"] = [_pr(14918)]
    mark = superseded.check(store, 7)
    found["prs"] = None
    assert superseded.check(store, 7) == mark
    assert store.load_issue(7).fix_run["superseded"] == mark


def test_a_check_that_finds_no_rival_clears_the_mark(store, found):
    _run(store)
    found["prs"] = [_pr(14918)]
    superseded.check(store, 7)
    found["prs"] = [_pr(14918, state="closed")]
    assert superseded.check(store, 7) is None
    issue = store.load_issue(7)
    assert "superseded" not in issue.fix_run
    assert fix_review.fix_status(issue)[0] == "reporter"


def test_an_issue_with_no_attempt_reports_its_rival_and_records_nothing(store, found):
    found["prs"] = [_pr(14918)]
    mark = superseded.check(store, 7)
    assert mark is not None and mark["pr"] == 14918
    assert store.load_issue(7).fix_run is None


def test_the_oldest_rival_names_the_mark(store, found):
    _run(store)
    found["prs"] = [_pr(14930), _pr(14918)]
    assert superseded.check(store, 7)["pr"] == 14918


def test_a_rival_that_claims_to_fix_the_issue_names_the_mark_ahead_of_a_mention(store, found):
    _run(store)
    found["prs"] = [_pr(14918, closes=True), _pr(14917)]
    assert superseded.check(store, 7)["pr"] == 14918


# --- the sweep -----------------------------------------------------------------------

def test_the_sweep_checks_only_attempts_waiting_on_their_next_step(store, found):
    for n in range(10, 18):
        _save(store, n)
    _run(store, 10)                                                   # question asked
    _run(store, 11, ending="fixed", question=None)                    # fixed, not proposed
    _run(store, 12, ending="fix-disputed",
         question={k: v for k, v in QUESTION.items() if k != "asked"})  # question drafted
    _run(store, 13, ending="sandbox", fault=True)                     # failed
    _run(store, 14, ending="no-fix", question=None)                   # declined
    _run(store, 15, ending="fixed", question=None, proposal={"pr": 9})  # its own PR open
    _run(store, 16)
    store.edit_issue(16).record_fix_request({"action": "answer", "status": "running",
                                             "source": "reporter"})  # running
    _run(store, 17, superseded={"pr": 1, "author": "x", "title": "t", "at": "t"})
    _save(store, 18, state="closed")
    _run(store, 18)
    found["prs"] = [_pr(14918)]
    assert superseded.sweep(store) == [13, 12, 11, 10]
    assert sorted(found["asked"]) == [10, 11, 12, 13]
    assert store.load_issue(10).fix_run["superseded"]["pr"] == 14918
    assert "superseded" not in store.load_issue(14).fix_run


def test_the_sweep_searches_at_most_its_limit(store, found):
    for n in range(10, 15):
        _save(store, n)
        _run(store, n)
    found["prs"] = []
    assert superseded.sweep(store, limit=3) == []
    assert found["asked"] == [14, 13, 12]
