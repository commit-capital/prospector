"""The public loop's writes: swapping a status label, creating it the first
time, and posting a comment once."""
from __future__ import annotations

import json
import subprocess

import pytest

from prospector_app.backend import activity, executor, safety_guard


@pytest.fixture
def gh(monkeypatch):
    state: dict = {"labels": [], "posts": [], "events": [], "existing": set(),
                   "comments": [], "fail": {}}
    monkeypatch.setattr(executor, "_FIX_LABELS_MADE", set())

    def label_bot_run(op, label, token, *, number=None):
        state["labels"].append((op, label, number))
        code, err = state["fail"].get(op, (0, ""))
        return subprocess.CompletedProcess([], code, "", err)

    monkeypatch.setattr(safety_guard, "label_bot_run", label_bot_run)
    monkeypatch.setattr("pipeline.gh.gh_json",
                        lambda path: {"name": "x"} if path.rsplit("/", 1)[-1] in
                        state["existing"] else None)

    def bot_run(argv, token):
        state["posts"].append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"html_url": "https://c/1"}), "")

    monkeypatch.setattr(executor, "bot_run", bot_run)
    monkeypatch.setattr("pipeline.gh.issue_comments", lambda n, timeout=60: [
        {"user": {"login": c["login"]}, "body": c["body"]} for c in state["comments"]])
    monkeypatch.setattr(activity, "record",
                        lambda kind, **f: state["events"].append((kind, f)) or f)
    return state


def test_a_label_swap_removes_the_old_label_and_creates_the_new_one_once(gh):
    res = executor.set_fix_label(9, add="needs answer", remove="fix in progress", issue=7,
                                 token="tok", dry_run=False)
    assert res["status"] == "executed" and res["pr"] == 9
    assert gh["labels"] == [("remove", "fix in progress", 9), ("create", "needs answer", None),
                            ("add", "needs answer", 9)]
    executor.set_fix_label(7, add="needs answer", remove=None, issue=7, token="tok",
                           dry_run=False)
    assert gh["labels"][-1] == ("add", "needs answer", 7)
    assert [c[0] for c in gh["labels"]].count("create") == 1
    assert gh["events"][0][0] == "issue-fix-label"


def test_a_label_already_gone_counts_as_removed(gh):
    gh["fail"]["remove"] = (1, "gh: Label does not exist (HTTP 404)")
    res = executor.set_fix_label(7, add=None, remove="couldn't fix", issue=7, token="tok",
                                 dry_run=False)
    assert res["status"] == "executed"


def test_a_label_that_cannot_be_created_is_an_error(gh):
    gh["fail"]["create"] = (1, "gh: Resource not accessible by integration (HTTP 403)")
    res = executor.set_fix_label(7, add="needs answer", remove=None, issue=7, token="tok",
                                 dry_run=False)
    assert res["status"] == "error" and ("add", "needs answer", 7) not in gh["labels"]


def test_a_label_write_without_a_token_is_a_dry_run(gh):
    res = executor.set_fix_label(7, add="needs answer", remove=None, issue=7, token=None,
                                 dry_run=False)
    assert res["status"] == "dry-run" and res["forced"] and gh["labels"] == []


def test_a_comment_posts_as_the_bot_unless_its_marker_is_already_there(gh, monkeypatch):
    monkeypatch.setenv("TRIAGE_BOT_LOGIN", "triagebot")
    res = executor.post_fix_comment(7, "Body\n<!-- m -->", marker="<!-- m -->", issue=7,
                                    comment_kind="no-fix", token="tok", dry_run=False)
    assert res["status"] == "executed" and res["url"] == "https://c/1"
    [argv] = gh["posts"]
    assert argv[2].endswith("/issues/7/comments") and argv[-1] == "body=Body\n<!-- m -->"
    gh["comments"].append({"login": "triagebot[bot]", "body": "Body\n<!-- m -->"})
    again = executor.post_fix_comment(7, "Body\n<!-- m -->", marker="<!-- m -->", issue=7,
                                      comment_kind="no-fix", token="tok", dry_run=False)
    assert again["status"] == "exists" and len(gh["posts"]) == 1
    assert {e[0] for e in gh["events"]} == {"issue-fix-comment"}


def test_another_author_s_copy_of_the_marker_does_not_count(gh, monkeypatch):
    monkeypatch.setenv("TRIAGE_BOT_LOGIN", "triagebot")
    gh["comments"].append({"login": "someone", "body": "<!-- m -->"})
    res = executor.post_fix_comment(7, "B <!-- m -->", marker="<!-- m -->", issue=7,
                                    comment_kind="no-fix", token="tok", dry_run=False)
    assert res["status"] == "executed"


def test_a_marker_quoted_inside_another_bot_comment_does_not_count(gh, monkeypatch):
    monkeypatch.setenv("TRIAGE_BOT_LOGIN", "triagebot")
    gh["comments"].append({"login": "triagebot[bot]", "body": "<!-- m --> and then more"})
    res = executor.post_fix_comment(7, "B\n<!-- m -->", marker="<!-- m -->", issue=7,
                                    comment_kind="no-fix", token="tok", dry_run=False)
    assert res["status"] == "executed"
