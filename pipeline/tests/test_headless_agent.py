import json
import pytest
from pipeline import headless_agent as ha


def test_parse_stream_accumulates_assistant_text_and_emits_events():
    events = []
    lines = [
        json.dumps({"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Hello "}}}),
        json.dumps({"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "world"}}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/b.json"}}]}}),
        json.dumps({"type": "result"}),
    ]
    text = ha.parse_stream(iter(lines), on_event=events.append)
    assert text == "Hello world"
    assert ("tool", "Read") in [(e[0], e[1]) for e in events]
    # the tool input is threaded through as the third element
    assert ("tool", "Read", {"file_path": "/tmp/b.json"}) in events


def test_tool_summary_renders_salient_input_one_line():
    assert ha.tool_summary("Bash", {"command": "gh pr diff 1234"}) == "Bash: gh pr diff 1234"
    assert ha.tool_summary("Read", {"file_path": "/tmp/cluster-203.json"}) == "Read: /tmp/cluster-203.json"
    assert ha.tool_summary("Grep", {"pattern": "TODO", "path": "src"}) == "Grep: TODO in src"
    # collapses newlines/extra whitespace
    assert ha.tool_summary("Bash", {"command": "git   log\n--oneline"}) == "Bash: git log --oneline"
    # no input → just the name
    assert ha.tool_summary("Glob", {}) == "Glob"
    # over-width is truncated with an ellipsis
    long = ha.tool_summary("Bash", {"command": "x" * 200}, width=30)
    assert len(long) == 30 and long.endswith("…")


def test_extract_json_handles_fenced_block():
    text = "Here is the result:\n```json\n{\"cluster_id\": 5, \"outcome\": \"close-out\"}\n```\ndone"
    assert ha.extract_json(text) == {"cluster_id": 5, "outcome": "close-out"}


def test_extract_json_handles_raw_trailing_object():
    text = "preamble\n{\"a\": 1, \"b\": [2, 3]}"
    assert ha.extract_json(text) == {"a": 1, "b": [2, 3]}


def test_extract_json_raises_on_no_json():
    with pytest.raises(ValueError):
        ha.extract_json("no json here at all")


def test_fill_substitutes_all_tokens():
    out = ha.fill("PR #__PR__ via __LENS__ lens", {"__PR__": 12, "__LENS__": "security"})
    assert out == "PR #12 via security lens"


def test_fill_is_single_pass_so_a_token_inside_a_value_is_not_re_substituted():
    # An untrusted PR title that itself contains another placeholder must stay literal.
    out = ha.fill('("__TITLE__") lens __LENS__',
                  {"__TITLE__": "fix __LENS__ handling", "__LENS__": "scope"})
    assert out == '("fix __LENS__ handling") lens scope'
