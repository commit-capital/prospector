"""Asking a disputed issue its question: the gate, the kept draft, the bot's
comment, and the held base."""
from __future__ import annotations

import json
import subprocess

import pytest

from issue_triage import dispute_question, fetch_issues, fix_lane, propose
from pipeline import verify_gc
from prospector_app.backend import activity, executor

QUESTION = {"question": "Should x read 2 or 3?",
            "options": [{"label": "A", "behavior": "2"}, {"label": "B", "behavior": "3"}],
            "default": "A", "default_reason": "the report says 2"}


@pytest.fixture
def issue(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGE_VERIFY_SCRATCH", str(tmp_path / "vs"))
    workdir = propose.result_dir(7)
    workdir.mkdir(parents=True)
    report = fix_lane.report_sha("t", "b")
    record = {"issue": 7, "ending": "fix-disputed", "report_sha": report,
              "base_sha": "a" * 40, "result": {"readings": [[0], [1]]}}
    (workdir / "result.json").write_text(json.dumps(record))
    state: dict = {"live": {"state": "open", "title": "t", "body": "b", "author": "r"},
                   "drafts": 0, "posts": [], "held": [], "events": []}
    monkeypatch.setattr(fetch_issues, "fetch_issue", lambda n: state["live"])

    def draft(title, body, result):
        state["drafts"] += 1
        return QUESTION

    monkeypatch.setattr(dispute_question, "draft", draft)

    def bot_run(argv, token):
        state["posts"].append(argv)
        return subprocess.CompletedProcess(argv, 0, "https://github.com/o/r/issues/7#c1\n", "")

    monkeypatch.setattr(executor, "bot_run", bot_run)
    monkeypatch.setattr(verify_gc, "hold", lambda sha: state["held"].append(sha))
    monkeypatch.setattr(activity, "record",
                        lambda kind, **f: state["events"].append((kind, f)) or f)
    state["qpath"] = workdir / "question.json"
    return state


def test_a_dry_run_drafts_and_keeps_the_question_without_posting(issue):
    res = executor.ask_issue_question(7, token=None, dry_run=True)
    assert res["status"] == "dry-run" and "Should x read 2 or 3?" in res["body"]
    assert issue["posts"] == [] and issue["held"] == []
    assert json.loads(issue["qpath"].read_text())["question"] == QUESTION
    assert issue["events"][0][0] == "issue-question"


def test_a_live_ask_posts_the_kept_draft_as_the_bot_and_holds_the_base(issue):
    executor.ask_issue_question(7, token=None, dry_run=True)
    res = executor.ask_issue_question(7, token="tok", dry_run=False)
    assert res["status"] == "executed" and issue["drafts"] == 1
    [argv] = issue["posts"]
    assert argv[:3] == ["gh", "issue", "comment"] and "Should x read 2 or 3?" in argv[-1]
    assert issue["held"] == ["a" * 40]
    posted = json.loads(issue["qpath"].read_text())["posted"]
    assert posted["url"].endswith("#c1") and posted["asked_at"]


def test_a_question_is_never_asked_twice(issue):
    executor.ask_issue_question(7, token="tok", dry_run=False)
    res = executor.ask_issue_question(7, token="tok", dry_run=False)
    assert res["status"] == "exists" and len(issue["posts"]) == 1


@pytest.mark.parametrize("change,why", [
    ({"state": "closed"}, "closed"),
    ({"body": "edited"}, "edited"),
])
def test_the_gate_blocks_before_any_agent_runs(issue, change, why):
    issue["live"] = {**issue["live"], **change}
    res = executor.ask_issue_question(7, token="tok", dry_run=False)
    assert res["status"] == "blocked" and why in res["detail"]
    assert issue["drafts"] == 0 and issue["posts"] == []
