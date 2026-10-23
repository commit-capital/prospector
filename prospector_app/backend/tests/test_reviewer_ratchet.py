"""reviewer_ratchet: label suggestions from stored outcomes, and each reviewer
configuration's scorecard, both derived on read."""
from __future__ import annotations

from dataclasses import replace

import pytest

from pipeline.storekit import VerdictRecord
from prospector_app.backend import reviewer_ratchet as rr

AT = "2026-10-20T00:00:00+00:00"
LATER = "2026-10-21T00:00:00+00:00"


def _row(reviewer: str, verdict: str, *, vid: int = 1, kind: str = "pr", n: int = 7,
         head: str | None = "h1", label: dict | None = None, at: str = AT) -> VerdictRecord:
    return VerdictRecord(id=vid, at=at, reviewer=reviewer, subject_kind=kind, subject=n,
                         head_sha=head, base_sha=None, prompt_sha="p", model="opus",
                         verdict=verdict, data={}, label=label)


def _facts(*rows: VerdictRecord, prs: dict | None = None, pr_events: dict | None = None,
           issues: dict | None = None, fix_runs: list | None = None) -> rr.Facts:
    return rr.Facts(rows=list(rows), prs=prs or {}, pr_events=pr_events or {},
                    issues=issues or {}, fix_runs=fix_runs or [])


def _merge(**over) -> dict:
    return {"kind": "merge", "pr": 7, "status": "merged", "dry_run": False, "at": LATER, **over}


def _issue(**over) -> dict:
    return {"state": "open", "state_reason": None, "fix_followup": None,
            "fix_run_proposal": None, "second_opinion": None, "fix_choice": None,
            "fix_scan": None, "links_github": None, "fix_thread": None, **over}


def _one(facts: rr.Facts) -> rr.Signal:
    [sig] = rr.signals(facts)
    return sig


def test_merged_over_yellow():
    sig = _one(_facts(_row("security", "YELLOW"),
                      pr_events={7: [_merge(security_override="known risk")]}))
    assert (sig.key, sig.suggests) == ("merged-over-yellow", "GREEN")


@pytest.mark.parametrize("reviewer,verdict,suggests", [
    ("security", "RED", "GREEN"), ("design", "fail", "pass")])
def test_merged_upstream(reviewer, verdict, suggests):
    sig = _one(_facts(_row(reviewer, verdict), prs={7: {"state": "merged", "head_sha": "h2"}}))
    assert (sig.key, sig.suggests) == ("merged-upstream", suggests)
    assert "reverted" in sig.why


def test_our_own_merge_is_not_merged_upstream():
    facts = _facts(_row("design", "fail"), prs={7: {"state": "merged", "head_sha": "h1"}},
                   pr_events={7: [_merge()]})
    assert rr.signals(facts) == []


def test_guidance_after_pass():
    run = {"pr": 7, "trigger": None,
           "stats": {"head_sha": "h1", "guidance": "reuse the pad helper"}}
    sig = _one(_facts(_row("design", "pass"), fix_runs=[run]))
    assert (sig.key, sig.suggests) == ("guidance-after-pass", "fail")
    assert "reuse the pad helper" in sig.why


def test_guidance_on_another_head_or_from_the_hunter_is_no_signal():
    other = {"pr": 7, "trigger": None, "stats": {"head_sha": "h9", "guidance": "g"}}
    hunter = {"pr": 7, "trigger": "autohunt", "stats": {"head_sha": "h1", "guidance": "g"}}
    assert rr.signals(_facts(_row("design", "pass"), fix_runs=[other, hunter])) == []


@pytest.mark.parametrize("followup", [
    {"closed_as": "closed"}, {"closed_as": "merged", "ci_attribution": {"files": {"a": "caused"}}}])
def test_closed_unmerged(followup):
    sig = _one(_facts(_row("fix-review", "safe", kind="issue", n=9),
                      issues={9: _issue(fix_followup=followup)}))
    assert (sig.key, sig.suggests) == ("closed-unmerged", "unsafe")


@pytest.mark.parametrize("over", [
    {"second_opinion": [{"pr": 15440, "verdict": "gap", "why": "misses the skip branch"}]},
    {"fix_choice": {"pr": 15440, "by": "op", "reason": "theirs is complete"}}])
def test_rival_better(over):
    sig = _one(_facts(_row("fix-review", "safe", kind="issue", n=9), issues={9: _issue(**over)}))
    assert (sig.key, sig.suggests) == ("rival-better", "unsafe")


def test_sent_back_by_an_operator():
    entry = {"at": LATER, "by": "Alex", "action": "send-back", "text": "test the call site"}
    sig = _one(_facts(_row("fix-review", "safe", kind="issue", n=9),
                      issues={9: _issue(fix_thread=[entry])}))
    assert (sig.key, sig.suggests) == ("sent-back", "unsafe")
    assert "test the call site" in sig.why


@pytest.mark.parametrize("by", ["followup", "hunter", "public"])
def test_an_automatic_send_back_is_no_signal(by):
    entry = {"at": LATER, "by": by, "action": "send-back", "text": "x"}
    assert rr.signals(_facts(_row("fix-review", "safe", kind="issue", n=9),
                             issues={9: _issue(fix_thread=[entry])})) == []


def test_a_send_back_before_the_run_is_no_signal():
    entry = {"at": "2026-10-01T00:00:00+00:00", "by": "Alex", "action": "send-back", "text": "x"}
    assert rr.signals(_facts(_row("fix-review", "safe", kind="issue", n=9),
                             issues={9: _issue(fix_thread=[entry])})) == []


def test_proposed_over_hold():
    sig = _one(_facts(_row("trust-boundary", "hold", kind="issue", n=9),
                      issues={9: _issue(fix_run_proposal={"pr": 20})}))
    assert (sig.key, sig.suggests) == ("proposed-over-hold", "pass")


@pytest.mark.parametrize("over", [
    {"fix_scan": {"status": "fixed", "fixed_by": 31}},
    {"links_github": [{"pr": 31, "state": "MERGED"}]}])
def test_fixed_elsewhere(over):
    sig = _one(_facts(_row("fixability", "skip", kind="issue", n=9), issues={9: _issue(**over)}))
    assert (sig.key, sig.suggests) == ("fixed-elsewhere", "go")


def test_labeled_rows_are_never_suggested():
    row = _row("security", "RED", label={"expected": "RED"})
    assert rr.signals(_facts(row, prs={7: {"state": "merged", "head_sha": "h1"}})) == []


def test_empty_facts_give_no_signals():
    assert rr.signals(_facts()) == []


def test_newest_first_and_one_per_row():
    old = _row("design", "fail", vid=1)
    new = replace(_row("security", "RED", vid=2), at=LATER)
    facts = _facts(old, new, prs={7: {"state": "merged", "head_sha": "h2"}})
    assert [s.verdict_id for s in rr.signals(facts)] == [2, 1]


def test_gather_makes_five_queries(monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    from issue_triage.issue_store import IssueStore
    from pipeline import verdict_log
    from pipeline.store import Store
    from prospector_app.backend import data, issue_data

    st = Store()
    monkeypatch.setattr(data, "_store", st)
    issue_data.set_store_root(None)
    for n in (9801, 9802, 9803):
        st.save_pr({"pr": n, "meta": {"title": "t", "author": "a", "state": "open",
                                      "draft": False, "head_sha": "h", "checked_at": AT}})
        IssueStore().save_issue({"issue": n, "meta": {"title": "t", "state": "open",
                                                      "updated_at": AT, "created_at": AT}})
        for kind, reviewer, verdict in (("pr", "design", "pass"), ("issue", "fixability", "go")):
            verdict_log.record(reviewer, verdict_log.Subject(kind, n), verdict=verdict,  # type: ignore[arg-type]
                               prompt_sha="p", model="m", data={}, inputs={}, store=st)
    since = rr.window_start()
    rr.gather(since)  # warm: engines, schema checks
    statements: list[str] = []

    def count(conn, cursor, statement, *args) -> None:
        statements.append(statement)
    event.listen(Engine, "before_cursor_execute", count)
    try:
        facts = rr.gather(since)
    finally:
        event.remove(Engine, "before_cursor_execute", count)
    assert {9801, 9802, 9803} <= set(facts.prs) and {9801, 9802, 9803} <= set(facts.issues)
    assert len(statements) == 5, statements
