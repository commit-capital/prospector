"""An author's answer to the blocking feedback on their PR's head (the design
findings the bot posted, a failing Greptile score, failing CI) makes the next
move ours, and the operator approves the design review past its findings or
hands the PR back."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pipeline import design_review, gates, review_policy, reviewers
from pipeline import store as S
from pipeline.storekit import now as _now
from pipeline.testsupport import greptile_entry, reviews_section, threat_section
from prospector_app.backend import activity, automation, data, filters, responses, service
from prospector_app.backend import app as appmod

HEAD = "abc123def456" + "0" * 28
POSTED = "2026-10-20T10:00:00+00:00"
LATER = "2026-10-21T10:00:00+00:00"


def _design() -> dict:
    return {"kind": "review", "reviewed_sha": HEAD, "observed_at": _now(), "score": None,
            "findings": [{"path": "src/a.py", "line": 3, "severity": "major", "category": "reuse",
                          "title": "dup helper", "body": "why", "resolved": False,
                          "outdated": False, "verification": {"verdict": "confirmed"}}],
            "summary": "s", "checks": [],
            "extra": {"rubric": reviewers.DESIGN_RUBRIC,
                      "comment": {"fingerprint": "x", "url": "u", "posted_at": POSTED}}}


def _responses(**sig: object) -> dict:
    return {"replied": False, "new_commits": False, "reopened": False, "resubmitted": False,
            "follow_ups": [], "last_response_at": LATER, "snippet": None, "commit_snippet": None,
            "by": None, "our_action": {"kind": "design-findings", "at": POSTED, "by": "op"},
            **sig}


def _save(st: S.Store, sig: dict | None, **sections: object) -> None:
    rec = {"pr": 5,
           "meta": {"title": "PR 5", "author": "al", "author_association": "CONTRIBUTOR",
                    "state": "open", "head_sha": HEAD, "updated_at": _now()},
           "signals": {"ci": "passing", "mergeable": True, "checked_at": _now(),
                       "against_head_sha": HEAD},
           "threat": threat_section(HEAD),
           "security": {"verdict": "GREEN", "against_head_sha": HEAD, "findings": [],
                        "checked_at": _now()},
           "reviews": reviews_section(HEAD, design=_design()), **sections}
    if sig is not None:
        rec["responses"] = sig
    st.save_pr(rec)
    data.refresh()


def _answer(pr) -> str | None:
    answer = gates.author_answer(pr)
    return answer.text if answer else None


@pytest.fixture
def st(tmp_path, monkeypatch) -> S.Store:
    store = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", store)
    monkeypatch.setattr(service, "changed_paths", lambda pr: ["src/a.py"])
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "gate")
    monkeypatch.setattr(activity, "operator",
                        lambda: {"name": "Alex Example", "email": None, "slug": "alex"})
    return store


def _pr(st: S.Store):
    pr = st.load_pr(5)
    assert pr is not None
    return pr


def test_an_authors_reply_after_our_design_comment_is_our_move(st):
    _save(st, _responses(replied=True, by="al", snippet="the helper predates this"))
    pr = _pr(st)
    assert _answer(pr) == "the helper predates this"
    standing = automation.classify(pr)
    assert standing is not None
    assert (standing["bucket"], standing["owner"], standing["column"]) == (
        "answered", "you", "act")
    assert "the helper predates this" in standing["reason"]


def test_a_push_the_bot_had_nothing_new_to_post_on_is_an_answer_too(st):
    _save(st, _responses(new_commits=True, commit_snippet="keep the helper: it is shared"))
    assert _answer(_pr(st)) == "keep the helper: it is shared"


def test_a_push_that_left_the_patch_as_it_was_is_no_answer(st):
    _save(st, _responses(new_commits=True, commit_snippet="rebase onto main"),
          patch={"id": "p1", "same_as": ["e" * 40], "against_head_sha": HEAD, "checked_at": _now()})
    assert _answer(_pr(st)) is None


@pytest.mark.parametrize("sig", [
    None,
    _responses(replied=True, by="maintainer-mo", snippet="please fix"),
    _responses(replied=True, by="al", our_action={"kind": "close", "at": POSTED}),
])
def test_no_answer_leaves_it_the_authors_move(st, sig):
    _save(st, sig)
    pr = _pr(st)
    assert _answer(pr) is None
    standing = automation.classify(pr)
    assert standing is not None and standing["bucket"] != "answered"


def test_an_ack_hands_it_back_until_a_newer_answer(st):
    _save(st, _responses(replied=True, by="al", snippet="no"),
          response_ack={"at": LATER, "by": "op"})
    assert _answer(_pr(st)) is None
    _save(st, _responses(replied=True, by="al", snippet="still no",
                         last_response_at="2026-10-22T10:00:00+00:00"),
          response_ack={"at": LATER, "by": "op"})
    assert _answer(_pr(st)) == "still no"


def test_the_home_card_filter_takes_design_answers_beside_author_returns(st):
    _save(st, _responses(replied=True, by="al", snippet="no"))
    row = {"automation": automation.classify(_pr(st)), "author_return": None}
    assert filters.matches(row, {"author_return": True})
    assert filters.matches(row, {"author_return": "answer"})
    assert not filters.matches(row, {"author_return": "reopened"})


def _greptile(score: int = 3, at: str = POSTED) -> dict:
    return reviews_section(HEAD, greptile={**greptile_entry(score, HEAD), "observed_at": at})


def _feedback_responses(**sig: object) -> dict:
    return _responses(our_action={"kind": gates.FEEDBACK_ACTION, "at": POSTED, "by": None},
                      **sig)


def test_a_reply_to_a_failing_greptile_score_is_our_move(st):
    _save(st, _feedback_responses(replied=True, by="al", snippet="that path is unreachable"),
          reviews=_greptile())
    pr = _pr(st)
    assert gates.author_answer(pr) == gates.Answer("review", "that path is unreachable")
    standing = automation.classify(pr)
    assert standing is not None
    assert (standing["bucket"], standing["answer"]) == ("answered", "review")
    assert "review score" in standing["reason"]
    row = {"automation": standing, "author_return": None}
    assert filters.matches(row, {"author_return": "answer"})


def test_a_reply_to_failing_ci_answers_the_ci(st):
    failing = {"ci": "failing", "mergeable": True, "checked_at": _now(), "against_head_sha": HEAD,
               "ci_failures": {"failures": [{"name": "e2e", "started_at": POSTED}]}}
    _save(st, _feedback_responses(replied=True, by="al", snippet="e2e is red on main too"),
          reviews=_greptile(score=5), signals=failing)
    assert gates.author_answer(_pr(st)) == gates.Answer("ci", "e2e is red on main too")


@pytest.mark.parametrize("reviews", [
    _greptile(score=5),                         # the score passes now
    _greptile(at="2026-10-22T10:00:00+00:00"),  # newer feedback the reply predates
])
def test_a_reply_to_feedback_that_no_longer_stands_is_no_answer(st, reviews):
    _save(st, _feedback_responses(replied=True, by="al", snippet="no"), reviews=reviews)
    assert gates.author_answer(_pr(st)) is None


def test_the_sweep_measures_a_contributors_pr_from_its_newest_head_feedback(st):
    _save(st, None, reviews=_greptile(at=LATER))
    acted = {5: {"at": POSTED, "kind": "comment", "by": "op"}}
    base = responses._with_feedback(acted, data.prs())
    assert base[5] == {"at": datetime.fromisoformat(LATER).isoformat(),
                       "kind": gates.FEEDBACK_ACTION, "by": None,
                       "reason": "Greptile scored it 3/5"}
    newer = {5: {"at": "2026-10-23T10:00:00+00:00", "kind": "comment", "by": "op"}}
    assert responses._with_feedback(newer, data.prs()) == newer
    _save(st, None, reviews=_greptile(at=LATER), meta={
        "title": "PR 5", "author": "mo", "author_association": "MEMBER", "state": "open",
        "head_sha": HEAD, "updated_at": _now()})
    assert responses._with_feedback({}, data.prs()) == {}


def test_an_answered_pr_never_closes_as_abandoned_or_takes_an_unasked_fix(st):
    _save(st, _responses(replied=True, by="al", snippet="no"))
    pr = _pr(st)
    old = datetime(2026, 10, 1, tzinfo=timezone.utc)
    live = gates.LivePr(HEAD, old, old, old, "success")
    ok, why = gates.abandoned_close(pr, live, old + timedelta(days=60))
    assert not ok and "answered the feedback" in why
    ok, why = gates.fix_huntable(pr, "fix", ["src/a.py"])
    assert not ok and "answered the feedback" in why


@pytest.fixture
def client() -> TestClient:
    return TestClient(appmod.app, raise_server_exceptions=False)


def test_approving_passes_the_design_bar_and_is_logged(st, client, monkeypatch):
    _save(st, _responses(replied=True, by="al", snippet="no"))
    assert _answer(_pr(st)) == "no"
    logged: list[dict] = []
    monkeypatch.setattr(activity, "record", lambda kind, **f: logged.append({"kind": kind, **f}))
    r = client.post("/api/reviews/design/approve/pr/5",
                    json={"head_sha": HEAD, "reason": "the helper is shared on purpose"})
    assert r.status_code == 200
    pr = _pr(st)
    override = reviewers.design_override(pr.review_entry(reviewers.DESIGN.id))
    assert override is not None
    assert (override["by"], override["reason"]) == ("Alex Example", "the helper is shared on purpose")
    assert review_policy.own_bar(pr, reviewers.DESIGN).status == reviewers.PASS
    assert reviewers.findings_for_fix(reviewers.DESIGN, pr.review_entry(reviewers.DESIGN.id),
                                      HEAD, None) == []
    assert _answer(pr) is None
    assert logged[-1]["kind"] == "design-approve" and logged[-1]["pr"] == 5
    assert logged[-1]["status"] == "recorded" and not activity.is_landed(logged[-1])


def test_a_new_review_replaces_the_approval(st, client):
    _save(st, None)
    assert client.post("/api/reviews/design/approve/pr/5",
                       json={"head_sha": HEAD, "reason": "fine"}).status_code == 200
    assert st.save_design_review(5, _design(), head_sha=HEAD)
    data.refresh()
    assert review_policy.own_bar(_pr(st), reviewers.DESIGN).status == reviewers.FAIL


def test_an_approval_never_lands_on_a_review_that_moved_under_it(st, monkeypatch):
    _save(st, None)
    prs = st._prs
    real = prs.save_if

    def raced(rec, stamp):
        monkeypatch.setattr(prs, "save_if", real)
        assert st.save_design_review(5, {**_design(), "summary": "newer"}, head_sha=HEAD)
        return real(rec, stamp)
    monkeypatch.setattr(prs, "save_if", raced)
    assert design_review.approve(st, 5, HEAD, "fine", by="op") is not None
    entry = _pr(st).review_entry(reviewers.DESIGN.id)
    assert entry is not None and entry["summary"] == "newer" and "override" not in entry


@pytest.mark.parametrize("payload, words", [
    ({"head_sha": "f" * 40, "reason": "fine"}, "reload"),
    ({"head_sha": HEAD, "reason": "  "}, "say why"),
])
def test_approving_is_refused_off_the_read_head_or_without_a_reason(st, client, payload, words):
    _save(st, None)
    r = client.post("/api/reviews/design/approve/pr/5", json=payload)
    assert r.status_code == 409 and words in r.json()["detail"]
    assert reviewers.design_override(_pr(st).review_entry(reviewers.DESIGN.id)) is None


def test_a_passing_review_holds_nothing_to_approve(st, client):
    _save(st, None)
    assert client.post("/api/reviews/design/approve/pr/5",
                       json={"head_sha": HEAD, "reason": "fine"}).status_code == 200
    r = client.post("/api/reviews/design/approve/pr/5", json={"head_sha": HEAD, "reason": "again"})
    assert r.status_code == 409 and "nothing to approve" in r.json()["detail"]


def test_the_sweep_keeps_the_newest_authored_commits_message():
    sig = responses.classify(POSTED, "design-findings", state="open", comments=[], reopens=[],
                             commit_dates=[LATER], authored=[
                                 {"committedDate": "2026-10-19T10:00:00+00:00", "message": "older"},
                                 {"committedDate": LATER, "message": "keep  the\nhelper"}])
    assert sig is not None and sig["commit_snippet"] == "keep the helper"


def test_a_merge_alone_leaves_no_commit_message():
    node = {"state": "OPEN", "author": {"login": "al"}, "comments": {"nodes": []},
            "timelineItems": {"nodes": []},
            "commits": {"nodes": [{"commit": {"committedDate": LATER, "oid": "m",
                                              "message": "Merge branch 'main'",
                                              "parents": {"totalCount": 2}}}]}}
    sig = responses._node_signals(node, {"at": POSTED, "kind": "design-findings"})
    assert sig is not None and sig["new_commits"] and sig["commit_snippet"] is None
