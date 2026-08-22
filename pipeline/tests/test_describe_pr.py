"""The description-rewriting agent driver: which review findings are about the
PR body, the template-section check, and the verdict validation. The agent is
mocked — run_agent is a subprocess boundary."""
from __future__ import annotations

import json

import pytest

from pipeline import describe_pr, headless_agent

TEMPLATE = "## Thinking Path\n\n## What Changed\n\n## Verification\n"
REQUIRED = ("Thinking Path", "What Changed", "Verification")


def _run(monkeypatch, reply: str, body: str = "Fixes the retry loop.", **over) -> tuple[dict, dict]:
    calls: dict = {}

    def fake_run_agent(prompt, *, allow_gh, cwd, edit_root=None, timeout=0,
                       on_event=None, system_prompt=None, model=None, allow=(),
                       env_extra=None):
        calls.update(prompt=prompt, allow_gh=allow_gh, cwd=cwd, edit_root=edit_root)
        return reply

    monkeypatch.setattr(headless_agent, "run_agent", fake_run_agent)
    kwargs = dict(pr=7, title="Fix the retry", body=body, diff="diff --git a b\n",
                  template=TEMPLATE, findings=[{"title": "PR description is missing "
                                                         "required template sections"}],
                  required=REQUIRED)
    kwargs.update(over)
    return describe_pr.describe(**kwargs), calls


class TestClassifier:
    @pytest.mark.parametrize("title", [
        "PR description is missing required template sections",
        "PR description does not follow the required template",
        "Incomplete PR description — required sections missing",
        "Pull request body uses unfilled template placeholders",
    ])
    def test_description_findings(self, title):
        assert describe_pr.is_description_nit({"title": title})

    @pytest.mark.parametrize("title", [
        "retry loop never exits", "missing null check on response.body",
        "The template literal is unterminated",
    ])
    def test_code_findings(self, title):
        assert not describe_pr.is_description_nit({"title": title, "body": "x"})

    def test_reads_the_first_line_of_the_body_when_there_is_no_title(self):
        assert describe_pr.is_description_nit(
            {"body": "**PR description is missing required template sections**\n\nmore"})


class TestSections:
    def test_names_the_missing_headings(self):
        body = "## Thinking Path\ntext\n### verification\nran it\n"
        assert describe_pr.missing_sections(body, REQUIRED) == ["What Changed"]

    def test_empty_requirement_is_always_met(self):
        assert describe_pr.missing_sections("anything", ()) == []


class TestDescribe:
    def test_returns_the_body_and_runs_read_only(self, monkeypatch):
        new = ("## Thinking Path\n> - a\n\n## What Changed\n- b\n\n## Verification\n"
               "Not stated by the author.\n\n## Original description\nFixes the retry loop.\n")
        out, calls = _run(monkeypatch, json.dumps({"body": new}))
        assert out == {"body": new}
        assert calls["edit_root"] is None and calls["allow_gh"] is False
        assert "Thinking Path" in calls["prompt"]

    def test_give_up_passes_through(self, monkeypatch):
        out, _ = _run(monkeypatch, json.dumps({"give_up": "the diff is binary"}))
        assert out == {"give_up": "the diff is binary"}

    def test_a_missing_required_section_is_refused(self, monkeypatch):
        new = "## Thinking Path\n> - a\n\n## Original description\nFixes the retry loop.\n"
        with pytest.raises(ValueError, match="What Changed"):
            _run(monkeypatch, json.dumps({"body": new}))

    def test_dropping_the_authors_text_is_refused(self, monkeypatch):
        new = "## Thinking Path\n> - a\n\n## What Changed\n- b\n\n## Verification\nn/a\n"
        with pytest.raises(ValueError, match="verbatim"):
            _run(monkeypatch, json.dumps({"body": new}))

    def test_an_empty_original_needs_no_carrying(self, monkeypatch):
        new = "## Thinking Path\n> - a\n\n## What Changed\n- b\n\n## Verification\nn/a\n"
        out, _ = _run(monkeypatch, json.dumps({"body": new}), body="")
        assert out == {"body": new}

    def test_garbage_is_an_error_not_a_body(self, monkeypatch):
        with pytest.raises(ValueError):
            _run(monkeypatch, "I could not decide.")
