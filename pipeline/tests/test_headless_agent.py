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


def test_flags_edit_root_scopes_edit_and_write(tmp_path):
    # A rule path with one leading slash resolves against the project root; the
    # double slash is the CLI's filesystem-absolute form, and a rule naming a
    # symlink never matches, so the root is named by its target.
    real = tmp_path / "wt"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    flags = ha._flags(False, edit_root=f"{link}/")
    allowed = flags[flags.index("--allowedTools") + 1]
    target = real.resolve()
    assert f"Edit(/{target}/**)" in allowed
    assert f"Write(/{target}/**)" in allowed
    assert f"Edit({link}/**)" not in allowed
    assert "Bash(git diff:*)" in allowed
    i = flags.index("--disallowedTools")
    disallowed = flags[i + 1:flags.index("--permission-mode")]
    assert "Edit" not in disallowed
    assert "Write" not in disallowed
    assert "Task" in disallowed


def test_flags_default_stays_read_only():
    flags = ha._flags(False)
    allowed = flags[flags.index("--allowedTools") + 1]
    assert "Edit" not in allowed
    assert "Bash(" not in allowed


def test_flags_never_admit_raw_gh_api_and_route_reads_through_gh_read():
    flags = ha._flags(True)
    allowed = flags[flags.index("--allowedTools") + 1].split(",")
    assert not any(a.startswith("Bash(gh api") for a in allowed)
    assert any(a.endswith("/prospector_app/agent/gh-read:*)") for a in allowed)
    i = flags.index("--disallowedTools")
    disallowed = flags[i + 1:flags.index("--permission-mode")]
    assert "Edit" in disallowed and "Write" in disallowed


class _FakeStdin:
    def __init__(self):
        self.chunks: list[str] = []
        self.closed = False

    def write(self, s: str) -> int:
        self.chunks.append(s)
        return len(s)

    def close(self) -> None:
        self.closed = True


class _FakeProc:
    def __init__(self, cmd):
        self.cmd = cmd
        self.pid = 4242
        self.returncode = 0
        self.stdin = _FakeStdin()
        self.stdout = iter([
            json.dumps({"type": "stream_event", "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "ok"}}}),
            json.dumps({"type": "result"}),
        ])

    def wait(self, timeout=None) -> int:
        return self.returncode


def test_run_agent_sends_the_prompt_over_stdin_not_argv(monkeypatch):
    """A prompt embedding a large PR diff exceeds the OS argv limit, so the
    prompt travels over stdin; argv carries only flags."""
    procs: list[_FakeProc] = []

    def fake_popen(cmd, **kwargs):
        assert kwargs.get("stdin") == ha.subprocess.PIPE
        proc = _FakeProc(cmd)
        procs.append(proc)
        return proc

    monkeypatch.setattr(ha.subprocess, "Popen", fake_popen)
    prompt = "review this diff\n" + "x" * 2_000_000
    text = ha.run_agent(prompt, allow_gh=False, cwd="/tmp")
    assert text == "ok"
    (proc,) = procs
    assert prompt not in proc.cmd
    assert all(len(arg) < 100_000 for arg in proc.cmd)
    assert "".join(proc.stdin.chunks) == prompt
    assert proc.stdin.closed


def _denial_proc(cmd, denials, text="ok"):
    proc = _FakeProc(cmd)
    proc.stdout = iter([
        json.dumps({"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text}}}),
        json.dumps({"type": "result", "permission_denials": denials}),
    ])
    return proc


def test_run_agent_raises_when_edits_inside_the_grant_are_denied(monkeypatch, tmp_path):
    """A permission denial of Edit/Write on a path inside edit_root means the
    grant never reached the agent — the machine's fault, not a verdict on the
    change — so run_agent raises instead of returning the agent's give-up."""
    wt = tmp_path / "wt"
    wt.mkdir()
    denials = [{"tool_name": "Write", "tool_use_id": "t1",
                "tool_input": {"file_path": str(wt / "src" / "a.ts"),
                               "content": "x"}}]
    monkeypatch.setattr(ha.subprocess, "Popen",
                        lambda cmd, **kw: _denial_proc(cmd, denials))
    with pytest.raises(ha.EditsBlockedError):
        ha.run_agent("go", allow_gh=False, cwd=str(wt), edit_root=str(wt))


def test_run_agent_raises_on_in_root_denial_through_a_symlinked_root(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    denials = [{"tool_name": "Edit", "tool_use_id": "t1",
                "tool_input": {"file_path": str(link / "a.py")}}]
    monkeypatch.setattr(ha.subprocess, "Popen",
                        lambda cmd, **kw: _denial_proc(cmd, denials))
    with pytest.raises(ha.EditsBlockedError):
        ha.run_agent("go", allow_gh=False, cwd=str(real), edit_root=str(real))


def test_run_agent_keeps_denials_outside_the_edit_root(monkeypatch, tmp_path):
    """An Edit the agent aimed outside its worktree is the lockdown working."""
    wt = tmp_path / "wt"
    wt.mkdir()
    denials = [{"tool_name": "Edit", "tool_use_id": "t1",
                "tool_input": {"file_path": "/etc/hosts"}}]
    monkeypatch.setattr(ha.subprocess, "Popen",
                        lambda cmd, **kw: _denial_proc(cmd, denials))
    assert ha.run_agent("go", allow_gh=False, cwd=str(wt),
                        edit_root=str(wt)) == "ok"


def test_run_agent_without_edit_root_keeps_edit_denials(monkeypatch):
    """With no edit grant, a denied Edit is the read-only lockdown working."""
    denials = [{"tool_name": "Edit", "tool_use_id": "t1",
                "tool_input": {"file_path": "/tmp/a.py"}}]
    monkeypatch.setattr(ha.subprocess, "Popen",
                        lambda cmd, **kw: _denial_proc(cmd, denials))
    assert ha.run_agent("go", allow_gh=False, cwd="/tmp") == "ok"


def test_run_agent_keeps_non_edit_denials_inside_the_root(monkeypatch, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    denials = [{"tool_name": "Bash", "tool_use_id": "t1",
                "tool_input": {"command": f"rm {wt}/a.py"}}]
    monkeypatch.setattr(ha.subprocess, "Popen",
                        lambda cmd, **kw: _denial_proc(cmd, denials))
    assert ha.run_agent("go", allow_gh=False, cwd=str(wt),
                        edit_root=str(wt)) == "ok"


def test_flags_allow_adds_rules_on_top_of_the_read_only_set():
    flags = ha._flags(False, allow=["Bash(/x/sandbox-check:*)"])
    allowed = flags[flags.index("--allowedTools") + 1].split(",")
    assert "Bash(/x/sandbox-check:*)" in allowed
    assert "Read" in allowed and not any(a.startswith("Edit") for a in allowed)


def test_run_agent_merges_env_extra_into_the_agents_environment(monkeypatch):
    seen: dict = {}

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs)
        return _FakeProc(cmd)

    monkeypatch.setattr(ha.subprocess, "Popen", fake_popen)
    ha.run_agent("hi", allow_gh=False, cwd="/tmp",
                 env_extra={"PROSPECTOR_CHECK_PR": "7"})
    assert seen["env"]["PROSPECTOR_CHECK_PR"] == "7"
    assert "PATH" in seen["env"]
