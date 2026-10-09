"""The public loop: the label and comments an attempt calls for, the writes a
pass makes against what it recorded, and the refresh that brings new issues in."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_triage import public_comments, public_loop
from issue_triage.issue_store import IssueStore
from issue_triage.public_loop import (
    COULDNT_FIX,
    IN_PROGRESS,
    ITERATING,
    NEEDS_ANSWER,
    READY,
    Queued,
)
from pipeline import gh, settings

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
FINISHED = "2026-10-01T11:00:00+00:00"
HEAD = "a" * 40
QUESTION = {"question": "2 or 3?", "options": [{"label": "A", "behavior": "2"},
                                               {"label": "B", "behavior": "3"}],
            "default": "A", "default_reason": "r"}


def _save(store: IssueStore, n: int, *, association: str | None = "MEMBER",
          state: str = "open") -> None:
    store.save_issue({"issue": n, "meta": {"title": f"bug {n}", "state": state, "body": "b",
                                           "updated_at": "2026-09-30T00:00:00Z",
                                           "author": "nicky", "author_association": association},
                      "links": {"candidates": [{"pr": 77, "how": "explicit"}]}})


def _run(**over) -> dict:
    return {"ending": "no-fix", "detail": "opus: no-fix", "finished": FINISHED,
            "started": "2026-10-01T10:30:00+00:00",
            "host": "studio", "question": None, "proposal": None,
            "root_cause": "the parser drops the flag", "candidates": [{"reproduces": True}],
            **over}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "worker_id", lambda: "studio")
    s = IssueStore(tmp_path)
    _save(s, 1)
    return s


def _label(store: IssueStore) -> str | None:
    return public_loop.label_for(store.load_issue(1))


# --- the label -----------------------------------------------------------------

def test_an_issue_with_no_attempt_carries_no_label(store):
    assert _label(store) is None


def test_a_running_request_or_a_result_awaiting_its_next_step_is_in_progress(store):
    store.edit_issue(1).record_fix_request({"action": "solve", "status": "running",
                                            "source": "hunter"})
    assert _label(store) == IN_PROGRESS
    store.edit_issue(1).record_fix_request({"action": "solve", "status": "done",
                                            "source": "hunter"})
    store.edit_issue(1).record_fix_run(_run(ending="fixed", patch="p"))
    assert _label(store) == IN_PROGRESS
    store.edit_issue(1).record_fix_run(_run(ending="fix-disputed", question=QUESTION))
    assert _label(store) == IN_PROGRESS


def test_a_question_asked_on_github_needs_an_answer(store):
    store.edit_issue(1).record_fix_run(_run(ending="fix-disputed", question={
        **QUESTION, "asked": {"at": FINISHED, "url": "u"}}))
    assert _label(store) == NEEDS_ANSWER


def test_an_open_proposal_iterates_until_the_follow_up_says_ready(store):
    store.edit_issue(1).record_fix_run(_run(ending="fixed", proposal={"pr": "9", "url": "u"}))
    assert _label(store) == ITERATING
    store.edit_issue(1).record_fix_followup({"pr": 9, "state": "ready", "head_sha": HEAD})
    assert _label(store) == READY
    store.edit_issue(1).record_fix_followup({"pr": 9, "state": "handed-back", "head_sha": HEAD})
    assert _label(store) == READY
    store.edit_issue(1).record_fix_request({"action": "send-back", "status": "running",
                                            "source": "followup"})
    assert _label(store) == ITERATING
    store.edit_issue(1).record_fix_followup({"pr": 9, "state": "done"})
    assert _label(store) is None


def test_a_failed_revision_leaves_an_open_proposal_iterating(store):
    store.edit_issue(1).record_fix_run(_run(ending="fixed", proposal={"pr": 9}))
    store.edit_issue(1).record_fix_followup({"pr": 9, "state": "watching"})
    store.edit_issue(1).record_fix_request({"action": "send-back", "status": "failed",
                                            "source": "followup", "reason": "no base"})
    assert _label(store) == ITERATING


def test_an_attempt_without_a_fix_could_not_fix_it_but_a_fault_or_cancel_says_nothing(store):
    store.edit_issue(1).record_fix_run(_run())
    assert _label(store) == COULDNT_FIX
    store.edit_issue(1).record_fix_run(_run(ending="cancelled"))
    assert _label(store) is None
    store.edit_issue(1).record_fix_run(_run(ending="sandbox", fault=True))
    assert _label(store) is None


def test_a_closed_issue_carries_no_label(store):
    store.edit_issue(1).record_fix_run(_run())
    store.edit_issue(1).record_live_state("closed", "completed")
    assert _label(store) is None


def test_the_pull_request_carries_its_issue_s_label(store):
    store.edit_issue(1).record_fix_run(_run(ending="fixed", proposal={"pr": 9}))
    assert public_loop.label_targets(store.load_issue(1)) == {1: ITERATING, 9: ITERATING}


# --- the comments and the queue ------------------------------------------------

def test_an_attempt_without_a_fix_gets_its_conclusion_while_it_is_recent(store):
    store.edit_issue(1).record_fix_run(_run(ending="not-reproduced"))
    [due] = public_loop.comments_due(store.load_issue(1), NOW)
    assert (due.number, due.kind) == (1, "not-reproduced")
    assert due.key == f"{FINISHED}:not-reproduced"
    assert public_loop.comments_due(store.load_issue(1), NOW + timedelta(days=2)) == []


def test_a_proposal_is_announced_on_the_issue_and_ready_on_the_pull_request(store):
    store.edit_issue(1).record_fix_run(_run(ending="fixed", proposal={"pr": 9},
                                            summary="Keep the flag"))
    store.edit_issue(1).record_fix_followup({"pr": 9, "state": "ready", "head_sha": HEAD,
                                             "judged": {"head_sha": HEAD, "reason": "green"}})
    opened, ready = public_loop.comments_due(store.load_issue(1), NOW)
    assert (opened.number, opened.kind, opened.key) == (1, "opened", "pr9:opened")
    assert "#9" in opened.body and "Keep the flag" in opened.body
    assert (ready.number, ready.kind, ready.key) == (9, "ready", f"pr9:{HEAD[:12]}:ready")


def test_a_fixed_attempt_opens_its_pull_request_and_a_drafted_question_is_asked(store):
    store.edit_issue(1).record_fix_run(_run(ending="fixed", patch="p"))
    assert public_loop.queue_due(store.load_issue(1), NOW) == Queued(
        "propose", f"{FINISHED}:propose")
    store.edit_issue(1).record_fix_run(_run(ending="fix-disputed", question=QUESTION))
    assert public_loop.queue_due(store.load_issue(1), NOW) == Queued(
        "ask-reporter", f"{FINISHED}:ask-reporter")
    assert public_loop.queue_due(store.load_issue(1), NOW + timedelta(days=4)) is None


CROSSING = {"kind": "network", "where": "src/x.ts:2", "what": "posts to a new host",
            "requested": True}


@pytest.mark.parametrize("association,crossing,held", [
    ("MEMBER", CROSSING, False),
    ("MEMBER", {**CROSSING, "requested": False}, True),
    ("NONE", CROSSING, True),
])
def test_a_fix_that_crosses_a_trust_boundary_waits_for_an_operator(
        store, monkeypatch, association, crossing, held):
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_PUBLIC_SCOPE", "all")
    _save(store, 1, association=association)
    store.edit_issue(1).record_fix_run(_run(ending="fixed", patch="p",
                                            boundary={"crossings": [crossing]}))
    issue = store.load_issue(1)
    assert (public_loop.queue_due(issue, NOW) is None) == held
    assert public_loop.label_for(issue) == (READY if held else IN_PROGRESS)


def test_the_hand_back_comment_keeps_the_reason_it_was_judged_with(store):
    store.edit_issue(1).record_fix_run(_run(ending="fixed", proposal={"pr": 9}))
    store.edit_issue(1).record_fix_followup({
        "pr": 9, "state": "handed-back", "head_sha": HEAD, "reason": "handed-back at this head",
        "judged": {"head_sha": HEAD, "reason": "2 revisions spent; greptile 3/5"}})
    _, back = public_loop.comments_due(store.load_issue(1), NOW)
    assert back.kind == "handed-back" and "2 revisions spent" in back.body


# --- a pass ----------------------------------------------------------------------

@pytest.fixture
def writes(monkeypatch):
    from prospector_app.backend import executor

    calls: list[tuple] = []

    def set_fix_label(number, *, add, remove, issue, token, dry_run):
        calls.append(("label", number, add, remove, dry_run))
        return {"status": "dry-run" if dry_run else "executed"}

    def post_fix_comment(number, body, *, marker, issue, comment_kind, token, dry_run):
        assert marker in body
        calls.append(("comment", number, comment_kind, dry_run))
        return {"status": "dry-run" if dry_run else "executed", "url": f"u{number}"}

    monkeypatch.setattr(executor, "set_fix_label", set_fix_label)
    monkeypatch.setattr(executor, "post_fix_comment", post_fix_comment)
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "t")
    return calls


def test_a_community_dry_run_keeps_maintainer_issues_live(store, writes, monkeypatch):
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_PUBLIC_SCOPE", "all-dry-run")
    _save(store, 2, association="NONE")
    for n in (1, 2):
        store.edit_issue(n).record_fix_run(_run())
    public_loop.sync(store, mode="live", now=NOW)
    assert ("label", 1, COULDNT_FIX, None, False) in writes
    assert ("label", 2, COULDNT_FIX, None, True) in writes
    assert store.load_issue(2).fix_public["dry"]["labels"] == {"2": COULDNT_FIX}
    assert not store.load_issue(2).fix_public.get("labels")


def test_a_pass_labels_and_comments_once(store, writes):
    store.edit_issue(1).record_fix_run(_run())
    assert public_loop.sync(store, mode="live", now=NOW) == 1
    assert writes == [("label", 1, COULDNT_FIX, None, False),
                      ("comment", 1, "no-fix", False)]
    pub = store.load_issue(1).fix_public
    assert pub["labels"] == {"1": COULDNT_FIX} and "lease" not in pub
    assert public_loop.sync(store, mode="live", now=NOW) == 0
    assert len(writes) == 2


def test_a_new_state_swaps_the_label(store, writes):
    store.edit_issue(1).record_fix_run(_run())
    public_loop.sync(store, mode="live", now=NOW)
    store.edit_issue(1).record_fix_request({"action": "solve", "status": "running",
                                            "source": "operator"})
    public_loop.sync(store, mode="live", now=NOW)
    assert writes[-1] == ("label", 1, IN_PROGRESS, COULDNT_FIX, False)


def test_a_pass_opens_the_pull_request_for_a_fixed_attempt_once(store, writes):
    store.edit_issue(1).record_fix_run(_run(ending="fixed", patch="p"))
    public_loop.sync(store, mode="live", now=NOW)
    req = store.load_issue(1).fix_request
    assert (req["action"], req["source"], req["status"]) == ("propose", "public", "queued")
    store.edit_issue(1).record_fix_request({**req, "status": "failed", "reason": "blocked"})
    public_loop.sync(store, mode="live", now=NOW)
    assert store.load_issue(1).fix_request["status"] == "failed"


def test_a_dry_run_notes_each_write_once_and_keeps_its_record_apart(store, writes):
    store.edit_issue(1).record_fix_run(_run())
    public_loop.sync(store, mode="dry-run", now=NOW)
    assert all(call[-1] is True for call in writes) and len(writes) == 2
    issue = store.load_issue(1)
    assert issue.fix_public["dry"]["labels"] == {"1": COULDNT_FIX}
    assert "labels" not in {k for k, v in issue.fix_public.items() if v and k != "dry"}
    assert [e["text"][:7] for e in issue.fix_thread] == ["Dry run", "Dry run"]
    public_loop.sync(store, mode="dry-run", now=NOW)
    assert len(writes) == 2
    public_loop.sync(store, mode="live", now=NOW)
    assert [c[-1] for c in writes[2:]] == [False, False]


def test_an_issue_out_of_scope_is_left_alone(store, writes):
    _save(store, 2, association="NONE")
    store.edit_issue(2).record_fix_run(_run())
    public_loop.sync(store, mode="live", now=NOW)
    assert writes == []


def test_scope_all_serves_every_issue(store, writes, monkeypatch):
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_PUBLIC_SCOPE", "all")
    _save(store, 2, association="NONE")
    store.edit_issue(2).record_fix_run(_run())
    public_loop.sync(store, mode="live", now=NOW)
    assert ("label", 2, COULDNT_FIX, None, False) in writes


def test_another_worker_s_lease_holds_the_issue(store, writes):
    store.edit_issue(1).record_fix_run(_run())
    store.edit_issue(1).record_fix_public({"lease": {
        "host": "laptop", "until": (NOW + timedelta(minutes=3)).isoformat()}})
    assert public_loop.sync(store, mode="live", now=NOW) == 0
    assert writes == []
    assert public_loop.sync(store, mode="live", now=NOW + timedelta(minutes=4)) == 1


def test_a_failed_write_backs_off_then_retries(store, writes, monkeypatch):
    from prospector_app.backend import executor

    store.edit_issue(1).record_fix_run(_run())
    monkeypatch.setattr(executor, "set_fix_label",
                        lambda *a, **k: {"status": "error", "detail": "HTTP 502"})
    public_loop.sync(store, mode="live", now=NOW)
    issue = store.load_issue(1)
    assert issue.fix_public["error"]["detail"] == "HTTP 502"
    assert "retrying later" in issue.fix_thread[-1]["text"]
    assert public_loop.sync(store, mode="live", now=NOW + timedelta(minutes=10)) == 0
    monkeypatch.undo()
    monkeypatch.setattr(settings, "worker_id", lambda: "studio")
    monkeypatch.setattr(executor, "set_fix_label", lambda *a, **k: {"status": "executed"})
    monkeypatch.setattr(executor, "post_fix_comment", lambda *a, **k: {"status": "executed"})
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "t")
    assert public_loop.sync(store, mode="live", now=NOW + timedelta(minutes=31)) == 1
    assert "error" not in store.load_issue(1).fix_public


def test_a_comment_that_fails_its_problems_is_recorded_and_not_posted(store, writes,
                                                                      monkeypatch):
    store.edit_issue(1).record_fix_run(_run())
    monkeypatch.setattr(public_comments, "problems", lambda body: ["the comment carries a link"])
    public_loop.sync(store, mode="live", now=NOW)
    assert [c[0] for c in writes] == ["label"]
    posted = store.load_issue(1).fix_public["posted"]
    assert list(posted.values())[0]["blocked"] == ["the comment carries a link"]


def test_live_without_a_bot_token_writes_nothing(store, writes, monkeypatch):
    from prospector_app.backend import executor

    store.edit_issue(1).record_fix_run(_run())
    monkeypatch.setattr(executor, "mint_bot_token", lambda: None)
    assert public_loop.sync(store, mode="live", now=NOW) == 0
    assert writes == []


# --- the refresh -----------------------------------------------------------------

def _raw(n: int, association: str, **over) -> dict:
    return {"number": n, "title": f"bug {n}", "body": "Steps: 1. run it", "state": "open",
            "user": {"login": "nicky"}, "author_association": association,
            "created_at": "2026-10-01T10:00:00Z", "updated_at": "2026-10-01T11:00:00Z",
            "labels": [], **over}


def _node(n: int, **over) -> dict:
    """The GraphQL read of issue `n` the refresh makes after the REST listing."""
    return {"number": n, "title": f"bug {n}", "body": "Steps: 1. run it", "state": "OPEN",
            "author": {"login": "nicky"}, "authorAssociation": "MEMBER",
            "createdAt": "2026-10-01T10:00:00Z", "updatedAt": "2026-10-01T11:00:00Z",
            "comments": {"totalCount": 0, "nodes": []}, **over}


def _graphql(nodes: dict[int, dict], asked: list[str]):
    def read(query: str, **kw) -> dict:
        asked.append(query)
        return {"data": {"repository": {
            f"i{n}": node for n, node in nodes.items() if f"i{n}:" in query}}}
    return read


def test_a_refresh_ingests_the_in_scope_issues_github_reports_updated(store, monkeypatch):
    urls: list[str] = []
    asked: list[str] = []
    monkeypatch.setattr(public_loop, "_refreshed", {})
    monkeypatch.setattr(gh, "gh_list", lambda url: urls.append(url) or [
        _raw(1, "MEMBER", title="bug 1, edited"), _raw(5, "MEMBER"), _raw(6, "NONE"),
        _raw(7, "MEMBER", pull_request={"url": "u"})])
    monkeypatch.setattr(gh, "gh_graphql", _graphql(
        {1: _node(1, title="bug 1, edited"), 5: _node(5)}, asked))
    assert public_loop.refresh(store, now=NOW) == 2
    assert "since=2026-09-30T12:00:00Z" in urls[0]
    assert "i1:" in asked[0] and "i5:" in asked[0]
    assert "i6:" not in asked[0] and "i7:" not in asked[0]
    assert store.load_issue(5).author_association == "MEMBER"
    assert store.load_issue(6) is None and store.load_issue(7) is None
    edited = store.load_issue(1)
    assert edited.title == "bug 1, edited"
    assert edited.candidate_prs == [{"pr": 77, "how": "explicit"}]
    public_loop.refresh(store, now=NOW + timedelta(minutes=10))
    assert "since=2026-10-01T11:59:00Z" in urls[1]


def test_an_unanswered_refresh_reads_the_same_window_again(store, monkeypatch):
    monkeypatch.setattr(public_loop, "_refreshed", {"since": "2026-10-01T11:00:00Z"})
    monkeypatch.setattr(gh, "gh_list", lambda url: None)
    assert public_loop.refresh(store, now=NOW) == 0
    assert public_loop._refreshed["since"] == "2026-10-01T11:00:00Z"


def test_an_unanswered_issue_read_reads_the_same_window_again(store, monkeypatch):
    monkeypatch.setattr(public_loop, "_refreshed", {"since": "2026-10-01T11:00:00Z"})
    monkeypatch.setattr(gh, "gh_list", lambda url: [_raw(5, "MEMBER")])
    monkeypatch.setattr(gh, "gh_graphql", lambda query, **kw: None)
    assert public_loop.refresh(store, now=NOW) == 0
    assert store.load_issue(5) is None
    assert public_loop._refreshed["since"] == "2026-10-01T11:00:00Z"


def test_the_bot_s_label_and_comment_leave_a_refreshed_issue_s_analysis_current(
        store, monkeypatch):
    from issue_triage import issue_freshness
    monkeypatch.setenv("TRIAGE_BOT_LOGIN", "triagebot[bot]")
    monkeypatch.setattr(public_loop, "_refreshed", {})
    asked: list[str] = []
    monkeypatch.setattr(gh, "gh_graphql", _graphql({1: _node(1)}, asked))
    monkeypatch.setattr(gh, "gh_list", lambda url: [_raw(1, "MEMBER")])
    public_loop.refresh(store, now=NOW)
    store.edit_issue(1).route_to("needs-human", "r")
    bot = {"createdAt": "2026-10-01T11:30:00Z", "lastEditedAt": None,
           "author": {"login": "triagebot", "__typename": "Bot"}}
    monkeypatch.setattr(gh, "gh_graphql", _graphql({1: _node(
        1, updatedAt="2026-10-01T11:31:00Z", labels={"nodes": [{"name": READY}]},
        comments={"totalCount": 1, "nodes": [bot]})}, asked))
    monkeypatch.setattr(gh, "gh_list", lambda url: [
        _raw(1, "MEMBER", updated_at="2026-10-01T11:31:00Z")])
    public_loop.refresh(store, now=NOW + timedelta(minutes=10))
    iss = store.load_issue(1)
    assert iss.updated_at == "2026-10-01T11:31:00Z" and iss.labels == [READY]
    assert issue_freshness.is_current(iss, "analysis")


# --- replies -----------------------------------------------------------------------

def _comment(login: str, body: str, *, at: str = "2026-10-01T11:30:00Z",
             association: str = "NONE", cid: int = 1) -> dict:
    return {"id": cid, "user": {"login": login}, "author_association": association,
            "body": body, "created_at": at}


@pytest.fixture
def replies(store, monkeypatch):
    from issue_triage import reply_router

    state: dict = {"comments": [], "reads": 0, "route": "retry", "routed": []}

    def gh_list(url, paginate=False):
        state["reads"] += 1
        return state["comments"]

    def route(context, rs):
        state["routed"].append((context, rs))
        return state["route"]

    monkeypatch.setattr(gh, "gh_list", gh_list)
    monkeypatch.setattr(reply_router, "route", route)
    monkeypatch.setenv("TRIAGE_BOT_LOGIN", "triagebot")
    store.edit_issue(1).record_fix_run(_run(report_sha=_sha("bug 1", "b")))
    _bump(store, "2026-10-01T11:31:00Z")
    return state


def _sha(title: str, body: str) -> str:
    from issue_triage import fix_lane
    return fix_lane.report_sha(title, body)


def _bump(store: IssueStore, updated: str, **meta) -> None:
    raw = store.load_issue(1).raw
    store.save_issue({**raw, "meta": {**raw["meta"], "updated_at": updated, **meta}})


def test_the_author_s_reply_with_detail_starts_another_attempt(store, replies):
    replies["comments"] = [_comment("nicky", "It only fails with --flag set.")]
    assert public_loop.answer_replies(store, mode="live", now=NOW) == 1
    issue = store.load_issue(1)
    req = issue.fix_request
    assert (req["action"], req["source"], req["requested_by"]) == ("solve", "public", "nicky")
    assert "guidance" not in req and "> It only fails with --flag set." in req["notes"]
    assert "the issue's author replied" in req["notes"]
    assert issue.fix_thread[-1]["by"] == "nicky"
    assert issue.fix_public["reattempts"] == 1
    assert public_loop.label_for(issue) == IN_PROGRESS


def test_a_maintainer_s_reply_is_guidance_and_the_author_s_a_note(store, replies):
    replies["comments"] = [_comment("nicky", "It only fails with --flag set."),
                           _comment("dotta", "Look at the parser", association="MEMBER",
                                    cid=2)]
    public_loop.answer_replies(store, mode="live", now=NOW)
    req = store.load_issue(1).fix_request
    assert "a maintainer replied" in req["guidance"] and "> Look at the parser" in req["guidance"]
    assert "--flag" not in req["guidance"]
    assert "> It only fails with --flag set." in req["notes"] and "parser" not in req["notes"]


def test_a_refused_attempt_says_nothing_and_replies_do_not_restart_it(store, replies):
    store.edit_issue(1).record_fix_run(_run(ending="refused", report_sha=_sha("bug 1", "b"),
                                            detail="the intake audit read it as malicious"))
    issue = store.load_issue(1)
    assert public_loop.label_for(issue) is None and public_loop.comments_due(issue, NOW) == []
    replies["comments"] = [_comment("nicky", "please try again")]
    assert public_loop.answer_replies(store, mode="live", now=NOW) == 0


def test_chatter_is_read_once_and_starts_nothing(store, replies):
    replies["comments"] = [_comment("nicky", "thanks!")]
    replies["route"] = "none"
    public_loop.answer_replies(store, mode="live", now=NOW)
    issue = store.load_issue(1)
    assert issue.fix_request is None and "nothing to act on" in issue.fix_thread[-1]["text"]
    reads = replies["reads"]
    public_loop.answer_replies(store, mode="live", now=NOW)
    assert replies["reads"] == reads


def test_an_unreadable_route_waits_for_the_next_pass(store, replies):
    replies["comments"] = [_comment("nicky", "try the other parser")]
    replies["route"] = None
    public_loop.answer_replies(store, mode="live", now=NOW)
    assert store.load_issue(1).fix_request is None
    replies["route"] = "retry"
    public_loop.answer_replies(store, mode="live", now=NOW)
    assert store.load_issue(1).fix_request["action"] == "solve"


def test_replies_wait_unread_while_the_ai_capacity_is_paused(store, replies):
    replies["comments"] = [_comment("nicky", "It only fails with --flag set.")]
    for _ in range(4):
        assert public_loop.answer_replies(store, mode="live", now=NOW,
                                          may_route=lambda: False) == 0
    assert replies["routed"] == [] and store.load_issue(1).fix_request is None
    assert public_loop.answer_replies(store, mode="live", now=NOW) == 1
    assert store.load_issue(1).fix_request["action"] == "solve"


def test_only_the_author_and_maintainers_count_and_never_the_bot(store, replies):
    replies["comments"] = [_comment("passerby", "+1 me too"),
                           _comment("triagebot[bot]", "Prospector's pipeline …"),
                           _comment("nicky", "old", at="2026-10-01T10:00:00Z")]
    public_loop.answer_replies(store, mode="live", now=NOW)
    assert replies["routed"] == [] and store.load_issue(1).fix_request is None
    replies["comments"] = [_comment("dotta", "Look at the parser", association="MEMBER")]
    _bump(store, "2026-10-01T11:45:00Z")
    public_loop.answer_replies(store, mode="live", now=NOW)
    assert store.load_issue(1).fix_request["requested_by"] == "dotta"


def test_a_letter_answer_is_left_to_the_question_poll_and_words_answer_it(store, replies):
    store.edit_issue(1).record_fix_run(_run(
        ending="fix-disputed", report_sha=_sha("bug 1", "b"),
        question={**QUESTION, "asked": {"at": "2026-10-01T11:10:00+00:00", "url": "u"}}))
    replies["comments"] = [_comment("nicky", "A")]
    public_loop.answer_replies(store, mode="live", now=NOW)
    assert replies["routed"] == [] and store.load_issue(1).fix_request is None
    replies["comments"] = [_comment("nicky", "Neither: it should read 4.", cid=2)]
    _bump(store, "2026-10-01T11:50:00Z")
    public_loop.answer_replies(store, mode="live", now=NOW)
    req = store.load_issue(1).fix_request
    assert req["action"] == "answer" and "answer" not in req
    assert "it should read 4" in req["notes"]


def test_a_maintainer_s_written_answer_answers_it(store, replies):
    store.edit_issue(1).record_fix_run(_run(
        ending="fix-disputed", report_sha=_sha("bug 1", "b"),
        question={**QUESTION, "asked": {"at": "2026-10-01T11:10:00+00:00", "url": "u"}}))
    replies["comments"] = [_comment("dotta", "Neither: it should read 4.", association="MEMBER")]
    public_loop.answer_replies(store, mode="live", now=NOW)
    req = store.load_issue(1).fix_request
    assert "it should read 4" in req["answer"]["text"] and "notes" not in req


def test_an_edited_report_starts_another_attempt_once(store, replies):
    _bump(store, "2026-10-01T11:40:00Z", body="b, with steps")
    public_loop.answer_replies(store, mode="live", now=NOW)
    issue = store.load_issue(1)
    assert issue.fix_request["action"] == "solve" and replies["routed"] == []
    assert "report was edited" in issue.fix_thread[-1]["text"]
    store.edit_issue(1).record_fix_request({**issue.fix_request, "status": "done"})
    public_loop.answer_replies(store, mode="live", now=NOW)
    assert store.load_issue(1).fix_public["reattempts"] == 1


def test_replies_start_at_most_three_attempts_then_a_cap_comment(store, replies, writes):
    store.edit_issue(1).record_fix_public({"reattempts": public_loop.MAX_REATTEMPTS})
    replies["comments"] = [_comment("nicky", "try again please")]
    public_loop.answer_replies(store, mode="live", now=NOW)
    issue = store.load_issue(1)
    assert issue.fix_request is None and issue.fix_public["capped_at"]
    public_loop.sync(store, mode="live", now=NOW)
    assert ("comment", 1, "capped", False) in writes
    _bump(store, "2026-10-01T11:55:00Z")
    replies["comments"] = [_comment("nicky", "and again", cid=3, at="2026-10-01T11:54:00Z")]
    assert public_loop.answer_replies(store, mode="live", now=NOW) == 0


def test_a_dry_run_routes_nothing_and_queues_nothing(store, replies):
    replies["comments"] = [_comment("nicky", "It only fails with --flag set.")]
    public_loop.answer_replies(store, mode="dry-run", now=NOW)
    issue = store.load_issue(1)
    assert replies["routed"] == [] and issue.fix_request is None
    assert issue.fix_thread[-1]["text"].startswith("Dry run: would route 1 new reply")


def test_an_open_proposal_s_replies_belong_to_the_follow_up(store, replies):
    store.edit_issue(1).record_fix_run(_run(ending="fixed", proposal={"pr": 9},
                                            report_sha=_sha("bug 1", "b")))
    replies["comments"] = [_comment("nicky", "change it")]
    assert public_loop.answer_replies(store, mode="live", now=NOW) == 0
    assert replies["reads"] == 0


def test_an_issue_that_leaves_scope_has_its_labels_taken_off_and_nothing_more(store, writes):
    store.edit_issue(1).record_fix_run(_run())
    public_loop.sync(store, mode="live", now=NOW)
    raw = store.load_issue(1).raw
    store.save_issue({**raw, "meta": {**raw["meta"], "author_association": "NONE"}})
    store.edit_issue(1).record_fix_run(_run(ending="fixed", patch="p"))
    public_loop.sync(store, mode="live", now=NOW)
    assert writes[-1] == ("label", 1, None, COULDNT_FIX, False)
    assert store.load_issue(1).fix_request is None


def test_a_pass_decides_from_the_issue_as_its_lease_found_it(store, writes, monkeypatch):
    store.edit_issue(1).record_fix_run(_run())
    stale = store.load_issue(1)
    store.edit_issue(1).record_fix_run(_run(ending="cancelled"))
    public_loop.sync_issue(store, stale, mode="live", token="t", host="studio", now=NOW)
    assert writes == []


def test_a_request_a_machine_fault_ended_runs_again_after_a_rest(store, writes):
    store.edit_issue(1).record_fix_run(_run(ending="sandbox", fault=True))
    store.edit_issue(1).record_fix_request({
        "action": "solve", "status": "done", "source": "public", "guidance": "try X",
        "notes": "it fails with --flag", "finished_at": FINISHED})
    issue = store.load_issue(1)
    assert public_loop.label_for(issue) == IN_PROGRESS
    assert public_loop.queue_due(issue, NOW - timedelta(minutes=30), {}) is None
    public_loop.sync(store, mode="live", now=NOW + timedelta(minutes=5))
    req = store.load_issue(1).fix_request
    assert (req["action"], req["status"], req["guidance"], req["notes"]) == (
        "solve", "queued", "try X", "it fails with --flag")
    spent = {"queued": {f"retry:{k}:solve": "t" for k in (1, 2, 3)}}
    assert public_loop.queue_due(store.load_issue(1), NOW, spent) is None



def test_an_edit_carries_the_replies_that_came_with_it(store, replies):
    replies["comments"] = [_comment("nicky", "Added the steps above.")]
    _bump(store, "2026-10-01T11:40:00Z", body="b, with steps")
    public_loop.answer_replies(store, mode="live", now=NOW)
    req = store.load_issue(1).fix_request
    assert "the report was edited" in req["notes"] and "> Added the steps" in req["notes"]
    assert replies["routed"] == []


def test_a_reply_posted_while_the_attempt_ran_is_still_routed(store, replies):
    replies["comments"] = [_comment("nicky", "It also needs --flag.", at="2026-10-01T10:45:00Z")]
    public_loop.answer_replies(store, mode="live", now=NOW)
    assert "--flag" in store.load_issue(1).fix_request["notes"]


def test_an_attempt_that_concluded_before_the_loop_read_it_starts_from_that_read(store,
                                                                                 replies):
    replies["comments"] = [_comment("nicky", "old news")]
    public_loop.answer_replies(store, mode="live", now=NOW + timedelta(days=2))
    issue = store.load_issue(1)
    assert issue.fix_request is None and replies["reads"] == 0
    assert issue.fix_public["replies_seen"] == (NOW + timedelta(days=2)).isoformat(
        timespec="seconds")


def test_a_refused_or_unreadable_reply_is_left_for_a_person(store, replies):
    replies["comments"] = [_comment("nicky", "please try again")]
    replies["route"] = "declined"
    public_loop.answer_replies(store, mode="live", now=NOW)
    assert "declined" in store.load_issue(1).fix_thread[-1]["text"]
    replies["comments"] = [_comment("nicky", "and again", cid=2, at="2026-10-01T11:50:00Z")]
    replies["route"] = None
    for k in range(public_loop.MAX_ROUTE_MISSES):
        _bump(store, f"2026-10-01T11:5{k + 1}:00Z")
        public_loop.answer_replies(store, mode="live", now=NOW)
    issue = store.load_issue(1)
    assert issue.fix_request is None and "leaving them for a person" in issue.fix_thread[-1]["text"]
    assert len(replies["routed"]) == 1 + public_loop.MAX_ROUTE_MISSES


def test_the_cap_comment_does_not_hide_a_later_attempt_s_comments(store):
    store.edit_issue(1).record_fix_run(_run(ending="fixed", proposal={"pr": 9}))
    kinds = [d.kind for d in public_loop.comments_due(store.load_issue(1), NOW,
                                                      {"capped_at": "t"})]
    assert kinds == ["opened", "capped"]
