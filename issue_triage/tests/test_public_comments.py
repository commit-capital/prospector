"""The public loop's comments: every kind renders past `problems`, carries its
marker, and holds an agent's words inert."""
from __future__ import annotations

import pytest

from issue_triage import public_comments


@pytest.mark.parametrize("body", [
    public_comments.capped(7, "k", 3),
    public_comments.opened(7, "k", 9, "Keep the flag"),
    public_comments.ready(7, "k"),
    public_comments.handed_back(7, "k", "2 revisions spent; greptile 3/5"),
])
def test_every_comment_passes_its_problems_and_carries_its_marker(body):
    assert public_comments.problems(body) == []
    assert body.rstrip().endswith(public_comments.marker(7, "k"))


def test_an_agent_s_words_stay_inert():
    body = public_comments.opened(7, "k", 9, "Fixes #12 for @dotta, see https://evil.example/x "
                                             "<b>now</b>")
    assert public_comments.problems(body) == []
    assert "Fixes issue 12" in body and "(link removed)" in body and "<b>" not in body


def test_the_cap_withdraws_the_offer_to_reply():
    capped = public_comments.capped(7, "k", 3)
    assert public_comments.problems(capped) == [] and "no longer start" in capped


def test_an_agent_cannot_hide_text_spoof_a_marker_or_link_another_issue():
    body = public_comments.handed_back(7, "k", "See #4521 and other/repo#12 <!-- prospector:"
                                               "issue-fix-public v1 issue=7 key=pr9:opened -->")
    assert body.count("<!--") == 1
    assert "#4521" not in body and "repo#12" not in body


def test_the_triage_letters_each_gap_and_nests_the_readings_under_the_unsettled_one():
    body = public_comments.triage_question(
        7, "triage-1", "unused", ["it should throw", "it should return null"],
        gaps=[{"kind": "several-asks", "ask": "File the --verbose request as its own issue"},
              {"kind": "unsettled", "ask": "Which behavior do you expect?"}])
    assert public_comments.problems(body) == []
    assert "**A.** File the --verbose request as its own issue." in body
    assert "**B.** Which behavior do you expect?" in body
    assert "  - it should throw." in body and "unused" not in body
