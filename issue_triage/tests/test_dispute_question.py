"""The question a disputed run asks, the comment around it, the answer that
resumes it, and the resume itself. The agent, GitHub and the lane are faked."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from issue_triage import cross_lane, dispute_question, fix_lane, propose, question
from pipeline import headless_agent, prove

RESULT = {
    "readings": [[0, 2], [1]],
    "candidates": [{"index": i, "model": "opus", "ending": None} for i in range(3)],
    "candidate_patches": [
        {"index": i, "test_patch": f"diff --git a/t{i}.test.ts b/t{i}.test.ts\n+expect(x).toBe({v})\n",
         "fix_patch": "", "verdict": {"summary": f"set x to {v}", "root_cause": "x"}}
        for i, v in ((0, 2), (1, 3), (2, 2))],
}
QUESTION = {"question": "When x is read, should it be 2 or 3?",
            "options": [{"label": "A", "behavior": "x reads 2"},
                        {"label": "B", "behavior": "x reads 3"}],
            "default": "A", "default_reason": "the report says 2"}


def _agent(monkeypatch, reply: dict) -> dict:
    seen: dict = {}

    def fake(prompt, **kw):
        seen.update(prompt=prompt, **kw)
        return "```json\n" + json.dumps(reply) + "\n```"

    monkeypatch.setattr(headless_agent, "run_agent", fake)
    return seen


def test_the_draft_hands_the_agent_every_reading_and_no_tools_beyond_its_workdir(monkeypatch):
    seen = _agent(monkeypatch, QUESTION)
    assert dispute_question.draft("x is wrong", "x should be 2", RESULT) == QUESTION
    assert "### Reading A" in seen["prompt"] and "### Reading B" in seen["prompt"]
    assert "toBe(3)" in seen["prompt"] and seen["allow_gh"] is False
    assert seen["cwd"] == seen["read_root"] and seen["env_allow"] == ()


@pytest.mark.parametrize("reply", [
    {**QUESTION, "options": QUESTION["options"][:1]},
    {**QUESTION, "default": "C"},
    {**QUESTION, "question": " "},
])
def test_a_malformed_question_is_refused(monkeypatch, reply):
    _agent(monkeypatch, reply)
    with pytest.raises(ValueError):
        dispute_question.draft("t", "b", RESULT)


def test_readings_that_do_not_differ_ask_nothing(monkeypatch):
    _agent(monkeypatch, {"no_question": "same behavior"})
    assert dispute_question.draft("t", "b", RESULT) == {"no_question": "same behavior"}


def _render(q: dict = QUESTION) -> str:
    return dispute_question.render(7, q, report_sha="0123456789abcdef",
                                   default_after=datetime(2026, 10, 4, tzinfo=timezone.utc))


def test_the_comment_carries_the_question_options_default_and_marker():
    body = _render()
    assert "**When x is read, should it be 2 or 3?**" in body
    assert "- **A**: x reads 2" in body and "- **B**: x reads 3" in body
    assert "goes with **A** after 2026-10-04" in body
    assert "<!-- prospector:issue-question v1 issue=7 report=0123456789abcdef options=A,B -->" in body
    assert dispute_question.problems(body) == []


def test_agent_text_in_the_comment_is_inert():
    body = _render({**QUESTION, "question": "Fixes #9? see https://evil.example @bob"})
    assert "evil.example" not in body and "@bob" not in body
    assert dispute_question.problems(body) == ["the comment carries a closing keyword"]


@pytest.mark.parametrize("text,label", [
    ("A", "A"), ("b", "B"), ("**A** — that's right", "A"), ("Option B please", "B"),
    ("A.\nbecause", "A"), ("C", None), ("Actually neither", None), ("", None),
])
def test_an_answer_names_an_option_on_its_first_line(text, label):
    assert dispute_question.parse_answer(text, ["A", "B"]) == label


def test_only_the_author_or_a_maintainer_answers(monkeypatch):
    comments = [
        {"user": {"login": "rando"}, "author_association": "NONE", "body": "B",
         "created_at": "2026-09-28T00:00:00Z"},
        {"user": {"login": "maint"}, "author_association": "MEMBER", "body": "hmm, unsure",
         "created_at": "2026-09-28T01:00:00Z"},
        {"user": {"login": "reporter"}, "author_association": "NONE", "body": "A",
         "created_at": "2026-09-28T02:00:00Z", "html_url": "u"},
    ]
    monkeypatch.setattr(dispute_question.gh, "gh_list", lambda path: comments)
    assert dispute_question.read_answer(7, asked_at="2026-09-27T00:00:00+00:00",
                                        options=["A", "B"], issue_author="reporter") == {
        "label": "A", "login": "reporter", "url": "u"}


def test_the_default_stands_only_after_the_wait():
    asked = "2026-09-20T00:00:00+00:00"
    assert not dispute_question.default_due(asked, datetime(2026, 9, 26, tzinfo=timezone.utc))
    assert dispute_question.default_due(asked, datetime(2026, 9, 27, tzinfo=timezone.utc))


# --- the resume ------------------------------------------------------------------

@pytest.fixture
def disputed(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(tmp_path / "vs"))
    workdir = propose.result_dir(7)
    workdir.mkdir(parents=True)
    report = fix_lane.report_sha("x is wrong", "x should be 2")
    record = {"issue": 7, "ending": "fix-disputed", "report_sha": report, "base_sha": "a" * 40,
              "base_tier": 1, "result": RESULT}
    (workdir / "result.json").write_text(json.dumps(record))
    asked = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    (workdir / "question.json").write_text(json.dumps({
        "report_sha": report, "question": QUESTION, "posted": {"url": "q", "asked_at": asked}}))
    state: dict = {"answer": None, "judged": [], "ledger": []}
    monkeypatch.setattr(question.fetch_issues, "fetch_issue", lambda n: {
        "state": "open", "title": "x is wrong", "body": "x should be 2", "author": "reporter"})
    monkeypatch.setattr(question.dispute_question, "read_answer",
                        lambda n, **kw: state["answer"])
    monkeypatch.setattr(question.prove, "held", lambda sha, tier: prove.PinnedBase(
        sha=sha, tier=tier, image="img", clone=tmp_path / "clone"))

    def judge(spec, *, workdir, result, reading, on_step):
        state["judged"].append((spec.base.tier, reading))
        return fix_lane.LaneResult(ending="fixed", fault=False, detail="ok",
                                   result={**result, "patch": "P"})

    monkeypatch.setattr(cross_lane, "judge_reading", judge)

    class Ledger:
        def append_run(self, rec):
            state["ledger"].append(rec)

    monkeypatch.setattr(question, "IssueStore", Ledger)
    state["workdir"] = workdir
    return state


def test_an_answer_resumes_the_run_on_its_reading(disputed):
    disputed["answer"] = {"label": "B", "login": "reporter", "url": "u"}
    assert question.resume(7) == 0
    assert disputed["judged"] == [(1, [1])]
    now = json.loads((disputed["workdir"] / "result.json").read_text())
    assert now["ending"] == "fixed" and now["answer"]["label"] == "B"
    assert json.loads((disputed["workdir"] / "disputed.json").read_text())["ending"] == \
        "fix-disputed"
    assert disputed["ledger"][0]["stats"]["answer"] == "B"


def test_without_an_answer_the_run_waits(disputed):
    assert question.resume(7) == 0
    assert disputed["judged"] == []


def test_after_the_wait_the_default_answers(disputed, monkeypatch):
    monkeypatch.setattr(question.dispute_question, "default_due", lambda asked: True)
    assert question.resume(7) == 0
    assert disputed["judged"] == [(1, [0, 2])]
    assert disputed["ledger"][0]["stats"]["answer_default"] is True


def test_a_question_says_what_a_reply_in_words_does():
    from datetime import datetime, timezone
    q = {"question": "2 or 3?", "options": [{"label": "A", "behavior": "2"},
                                            {"label": "B", "behavior": "3"}],
         "default": "A", "default_reason": "r"}
    when = datetime(2026, 10, 8, tzinfo=timezone.utc)
    held = dispute_question.render(7, q, report_sha="s", default_after=when)
    loop = dispute_question.render(7, q, report_sha="s", default_after=when, retry_on_reply=True)
    assert "a maintainer will take it from there" in held
    assert "the pipeline will try again with it" in loop
    assert dispute_question.problems(loop) == []
