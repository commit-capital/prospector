"""The public loop's comments: every kind renders past `problems`, carries its
marker, and holds an agent's words inert."""
from __future__ import annotations

import pytest

from issue_triage import public_comments

RUN = {"ending": "fix-unproven", "detail": "1 independent reproduction(s); an agreed fix needs 2",
       "root_cause": "The parser drops the flag.", "candidates": [{"reproduces": True}]}
PINNED = ("@acme/adapter-local src/index.test.ts > local adapter metadata > "
          "advertises current models without changing the default")
PINNED_RUN = {"ending": "fix-pinned", "detail": f"2 fix(es) pass all 2 reproductions: {PINNED}",
              "candidates": [{"reproduces": True}],
              "agreement": {"reproductions": [0, 2], "agreed": [],
                            "pinned": {"agreed": [0, 2], "tests": [PINNED]}}}


@pytest.mark.parametrize("body", [
    public_comments.conclusion(7, "k", RUN),
    public_comments.conclusion(7, "k", {**RUN, "ending": "not-reproduced"}),
    public_comments.conclusion(7, "k", {**RUN, "ending": "not-a-defect"}),
    public_comments.conclusion(7, "k", {"ending": "fix-disputed"}),
    public_comments.conclusion(7, "k", PINNED_RUN),
    public_comments.opened(7, "k", 9, "Keep the flag"),
    public_comments.ready(7, "k"),
    public_comments.handed_back(7, "k", "2 revisions spent; greptile 3/5"),
])
def test_every_comment_passes_its_problems_and_carries_its_marker(body):
    assert public_comments.problems(body) == []
    assert body.rstrip().endswith(public_comments.marker(7, "k"))


def test_each_ending_without_a_fix_gets_its_own_comment():
    assert public_comments.kind_for("wrong-symptom") == "not-reproduced"
    assert public_comments.kind_for("not-a-defect") == "not-a-defect"
    assert public_comments.kind_for("fix-rejected") == "no-fix"
    body = public_comments.conclusion(7, "k", RUN)
    assert "It did reproduce the problem." in body
    assert "Its reading of the cause: The parser drops the flag." in body
    assert "not proven by every check (1 independent" in body


def test_an_agent_s_words_stay_inert():
    run = {**RUN, "root_cause": "Fixes #12 for @dotta, see https://evil.example/x <b>now</b>"}
    body = public_comments.conclusion(7, "k", run)
    assert public_comments.problems(body) == []
    assert "Fixes issue 12" in body and "(link removed)" in body and "<b>" not in body


def test_a_conclusion_invites_a_reply_and_the_cap_withdraws_the_offer():
    assert "starts another attempt" in public_comments.conclusion(7, "k", RUN)
    capped = public_comments.capped(7, "k", 3)
    assert public_comments.problems(capped) == [] and "no longer start" in capped


def test_an_agent_cannot_hide_text_spoof_a_marker_or_link_another_issue():
    run = {**RUN, "root_cause": "See #4521 and other/repo#12 "
                                "<!-- prospector:issue-fix-public v1 issue=7 key=pr9:opened -->"}
    body = public_comments.conclusion(7, "k", run)
    assert "<!-- prospector" not in body.splitlines()[0] and body.count("<!--") == 1
    assert "#4521" not in body and "repo#12" not in body


def test_a_pinned_conclusion_names_the_existing_test_and_what_lets_a_fix_through():
    body = public_comments.conclusion(7, "k", PINNED_RUN)
    assert public_comments.kind_for("fix-pinned") == "no-fix"
    assert "- @\u200bacme/adapter-local src/index.test.ts > local adapter metadata > " \
           "advertises current models without changing the default" in body
    assert "never changes a repository's own tests" in body
    assert "once that change has landed starts another attempt" in body
