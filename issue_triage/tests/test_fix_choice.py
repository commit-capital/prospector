from __future__ import annotations

from datetime import datetime, timezone

import pytest

from issue_triage import fix_choice, fix_review, public_loop
from issue_triage.issue_store import IssueStore
from pipeline.store import Store


@pytest.fixture
def stores(tmp_path):
    issues, prs = IssueStore(tmp_path / "issues"), Store(tmp_path / "prs")
    issues.save_issue({"issue": 7, "meta": {"title": "x is wrong", "state": "open",
                                            "updated_at": "2026-09-01T00:00:00Z",
                                            "author": "reporter"}})
    for n, state in ((12, "open"), (13, "closed")):
        prs.save_pr({"pr": n, "meta": {"title": f"pr {n}", "state": state, "author": "contrib",
                                       "head_sha": "a" * 40}})
    return issues, prs


def _fixed(issues: IssueStore, **over) -> None:
    issues.edit_issue(7).record_fix_run({"ending": "fixed", "host": "s", "patch": "p",
                                         "finished": datetime.now(timezone.utc).isoformat(),
                                         **over})


def test_going_with_a_pr_stops_our_fix_and_backs_the_pr(stores):
    issues, prs = stores
    _fixed(issues)
    fix_review.queue(issues, 7, "send-back", by="op", guidance="g")
    assert fix_choice.choose(issues, prs, 7, 12, by="op", reason="handles skipPermissions") == (
        True, "going with #12")
    issue = issues.load_issue(7)
    assert issue.fix_request["status"] == "cancelled"
    assert issue.fix_choice["pr"] == 12 and issue.fix_thread[-1]["text"].startswith("Going with #12")
    assert prs.load_pr(12).section("backing")["issue"] == 7
    assert fix_review.fix_status(issue) == ("chosen", "going with #12")
    assert fix_review.queue(issues, 7, "propose", by="public")[0] is False
    now = datetime.now(timezone.utc)
    assert public_loop.queue_due(issue, now) is None and public_loop.label_for(issue) is None


@pytest.mark.parametrize("setup,pr,why", [
    (lambda i: None, 13, "not an open pull request"),
    (lambda i: None, 99, "not an open pull request"),
    (lambda i: i.edit_issue(7).record_fix_request(
        {"action": "solve", "status": "running", "source": "operator"}), 12,
     "running"),
    (lambda i: _fixed(i, proposal={"pr": 40, "url": "u"}), 12, "our fix is open as #40"),
])
def test_a_choice_that_does_not_fit_is_refused(stores, setup, pr, why):
    issues, prs = stores
    setup(issues)
    ok, reason = fix_choice.choose(issues, prs, 7, pr, by="op", reason=None)
    assert not ok and why in reason and issues.load_issue(7).fix_choice is None


def test_the_choice_lapses_once_the_pr_closes_unmerged(stores):
    issues, prs = stores
    _fixed(issues)
    fix_choice.choose(issues, prs, 7, 12, by="op", reason=None)
    issues.set_issue_section(7, "links", {"github": [{"pr": 12, "state": "closed"}]})
    issue = issues.load_issue(7)
    assert fix_choice.chosen(issue) is None and fix_review.fix_status(issue)[0] == "review"


def test_undo_lets_the_factory_fix_it_again(stores):
    issues, prs = stores
    fix_choice.choose(issues, prs, 7, 12, by="op", reason=None)
    assert fix_choice.undo(issues, prs, 7, by="op") == (True, "no longer going with #12")
    assert issues.load_issue(7).fix_choice is None and prs.load_pr(12).section("backing") is None
    assert fix_review.queue(issues, 7, "solve", by="op") == (True, "solve queued")
    assert fix_choice.undo(issues, prs, 7, by="op")[0] is False


def test_the_bot_s_comment_recommends_the_pr_for_the_issue():
    body, marker = fix_choice.comment(7, 12)
    assert "recommends this pull request as the fix for #7" in body
    assert body.rstrip().endswith(marker)
