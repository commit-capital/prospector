"""Routing the words people wrote after an attempt: quoted as data, and only a
well-formed answer counts."""
from __future__ import annotations

import pytest

from issue_triage import reply_router
from issue_triage.reply_router import Reply
from pipeline import headless_agent


def test_replies_are_quoted_under_their_authors():
    text = reply_router.quoted([Reply(1, "nicky", "line one\nline two", "t"),
                                Reply(2, "dotta", "rename", "t", where="a.ts:3")])
    assert text == "nicky:\n> line one\n> line two\n\ndotta on a.ts:3:\n> rename"


@pytest.mark.parametrize(("answer", "want"), [
    ('```json\n{"route": "retry"}\n```', "retry"),
    ('```json\n{"route": "none"}\n```', "none"),
    ('```json\n{"route": "maybe"}\n```', None),
])
def test_only_a_well_formed_route_counts(monkeypatch, answer, want):
    prompts = []
    monkeypatch.setattr(headless_agent, "run_agent",
                        lambda prompt, **kw: prompts.append((prompt, kw)) or answer)
    assert reply_router.route("it ended no-fix", [Reply(1, "nicky", "try X", "t")]) == want
    prompt, kw = prompts[0]
    assert "> try X" in prompt and kw["allow_gh"] is False and kw["env_allow"] == ()


def test_an_outage_propagates_and_a_refusal_is_its_own_answer(monkeypatch):
    def down(prompt, **kw):
        raise headless_agent.AgentUnavailable("not logged in")
    monkeypatch.setattr(headless_agent, "run_agent", down)
    with pytest.raises(headless_agent.AgentUnavailable):
        reply_router.route("ctx", [Reply(1, "nicky", "try X", "t")])

    def refused(prompt, **kw):
        raise headless_agent.AgentDeclined("refused")
    monkeypatch.setattr(headless_agent, "run_agent", refused)
    assert reply_router.route("ctx", [Reply(1, "nicky", "try X", "t")]) == "declined"
    assert reply_router.route("ctx", []) == "none"
