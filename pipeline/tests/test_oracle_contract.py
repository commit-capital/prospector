from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import headless_agent
from pipeline.evals import oracle_contract

_FAILING = ["app.test.ts > list > returns 422 for a bad id",
            "app.test.ts > list > treats null as unassigned"]


def _answer(*bases: str, names: list[str] = _FAILING) -> str:
    return "```json\n" + json.dumps({"tests": [
        {"test": n, "basis": b, "why": "because"} for n, b in zip(names, bases, strict=True)
    ]}) + "\n```"


def _judge(tmp_path: Path, reply: str | Exception, failing: list[str] | None = _FAILING
           ) -> tuple[dict, list[str]]:
    prompts: list[str] = []

    def run(prompt: str) -> str:
        prompts.append(prompt)
        if isinstance(reply, Exception):
            raise reply
        return reply

    out = oracle_contract.judge(title="List 500s", body="Return 400 or []", test_hunks="+it()",
                                failing=failing, cache_dir=tmp_path / "cache", run=run)
    return out, prompts


def test_every_test_beyond_the_report_is_the_maintainers(tmp_path) -> None:
    out, _ = _judge(tmp_path, _answer("beyond", "allowed"))
    assert out["contract"] == "maintainer"


def test_one_stated_test_makes_the_failure_the_reports(tmp_path) -> None:
    out, _ = _judge(tmp_path, _answer("beyond", "stated"))
    assert out["contract"] == "report"


def test_a_failing_test_left_unanswered_counts_as_the_reports(tmp_path) -> None:
    out, _ = _judge(tmp_path, _answer("beyond", names=_FAILING[:1]))
    assert out["contract"] == "report"


def test_names_match_across_whitespace(tmp_path) -> None:
    spaced = [n.replace(" > ", "  >  ") for n in _FAILING]
    out, _ = _judge(tmp_path, _answer("beyond", "allowed", names=spaced))
    assert out["contract"] == "maintainer"


def test_an_unparsed_failing_set_is_unknown_without_a_run(tmp_path) -> None:
    out, prompts = _judge(tmp_path, _answer("beyond", "beyond"), failing=None)
    assert out["contract"] == "unknown"
    assert prompts == []


def test_a_malformed_answer_is_unknown_and_not_cached(tmp_path) -> None:
    out, prompts = _judge(tmp_path, "no json here")
    assert out["contract"] == "unknown"
    assert len(prompts) == 2  # json_reply asks once more
    assert not (tmp_path / "cache").exists()


def test_a_run_that_did_not_finish_is_unknown(tmp_path) -> None:
    out, _ = _judge(tmp_path, RuntimeError("timed out"))
    assert out["contract"] == "unknown"
    assert "timed out" in out["reason"]


def test_an_agent_outage_propagates(tmp_path) -> None:
    with pytest.raises(headless_agent.AgentUnavailable):
        _judge(tmp_path, headless_agent.AgentUnavailable("not logged in"))


def test_a_second_judgment_of_the_same_inputs_reads_the_cache(tmp_path) -> None:
    first, _ = _judge(tmp_path, _answer("beyond", "allowed"))
    second, prompts = _judge(tmp_path, _answer("stated", "stated"))
    assert second == first
    assert prompts == []


def test_the_prompt_carries_the_report_and_the_failing_names(tmp_path) -> None:
    _, prompts = _judge(tmp_path, _answer("beyond", "allowed"))
    assert "Return 400 or []" in prompts[0]
    assert all(n in prompts[0] for n in _FAILING)
