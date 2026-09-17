import json
import os

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


def test_extract_json_accepts_a_raw_newline_inside_a_string():
    text = 'Done.\n{"summary": "Bound the loop.\nAdded a cap.", "changes": []}'
    assert ha.extract_json(text) == {"summary": "Bound the loop.\nAdded a cap.", "changes": []}
    fenced = '```json\n{"give_up": "line one\n\tline two"}\n```'
    assert ha.extract_json(fenced) == {"give_up": "line one\n\tline two"}


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
    flags = ha._flags(False, edit_root=f"{link}/", read_root=str(link))
    allowed = flags[flags.index("--allowedTools") + 1]
    target = real.resolve()
    assert f"Edit(/{target}/**)" in allowed
    assert f"Write(/{target}/**)" in allowed
    assert f"Edit({link}/**)" not in allowed
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
        ha.run_agent("go", allow_gh=False, cwd=str(wt), edit_root=str(wt),
                     read_root=str(wt))


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
        ha.run_agent("go", allow_gh=False, cwd=str(real), edit_root=str(real),
                     read_root=str(real))


def test_run_agent_keeps_denials_outside_the_edit_root(monkeypatch, tmp_path):
    """An Edit the agent aimed outside its worktree is the lockdown working."""
    wt = tmp_path / "wt"
    wt.mkdir()
    denials = [{"tool_name": "Edit", "tool_use_id": "t1",
                "tool_input": {"file_path": "/etc/hosts"}}]
    monkeypatch.setattr(ha.subprocess, "Popen",
                        lambda cmd, **kw: _denial_proc(cmd, denials))
    assert ha.run_agent("go", allow_gh=False, cwd=str(wt), edit_root=str(wt),
                        read_root=str(wt)) == "ok"


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
    assert ha.run_agent("go", allow_gh=False, cwd=str(wt), edit_root=str(wt),
                        read_root=str(wt)) == "ok"


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


_EXPIRED = ("Failed to authenticate. API Error: 401 OAuth access token has expired. "
            "Re-authenticate to continue.")


def _failing_proc(cmd, lines, rc=1):
    proc = _FakeProc(cmd)
    proc.returncode = rc
    proc.stdout = iter(lines)
    return proc


def _delta(text):
    return json.dumps({"type": "stream_event", "event": {
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}})


def test_run_agent_raises_agent_unavailable_on_an_error_result(monkeypatch):
    lines = [json.dumps({"type": "result", "is_error": True, "result": _EXPIRED})]
    monkeypatch.setattr(ha.subprocess, "Popen", lambda cmd, **kw: _failing_proc(cmd, lines))
    with pytest.raises(ha.AgentUnavailable, match="OAuth access token has expired"):
        ha.run_agent("go", allow_gh=False, cwd="/tmp")


def test_run_agent_raises_agent_unavailable_on_a_plain_text_complaint(monkeypatch):
    lines = ["Not logged in. Please run /login"]
    monkeypatch.setattr(ha.subprocess, "Popen", lambda cmd, **kw: _failing_proc(cmd, lines))
    with pytest.raises(ha.AgentUnavailable, match="Not logged in"):
        ha.run_agent("go", allow_gh=False, cwd="/tmp")


def test_run_agent_raises_agent_unavailable_on_message_text_without_a_result(monkeypatch):
    lines = [_delta(_EXPIRED)]
    monkeypatch.setattr(ha.subprocess, "Popen", lambda cmd, **kw: _failing_proc(cmd, lines))
    with pytest.raises(ha.AgentUnavailable):
        ha.run_agent("go", allow_gh=False, cwd="/tmp")


def test_run_agent_does_not_read_an_outage_into_a_completed_runs_prose(monkeypatch):
    lines = [_delta("the fix guards against an Invalid API key response"),
             json.dumps({"type": "result", "is_error": False})]
    monkeypatch.setattr(ha.subprocess, "Popen", lambda cmd, **kw: _failing_proc(cmd, lines))
    with pytest.raises(RuntimeError) as info:
        ha.run_agent("go", allow_gh=False, cwd="/tmp")
    assert not isinstance(info.value, ha.AgentUnavailable)


def test_run_agent_keeps_an_ordinary_failure_a_runtime_error(monkeypatch):
    lines = [_delta("segfault in the middle"), json.dumps({"type": "result"})]
    monkeypatch.setattr(ha.subprocess, "Popen", lambda cmd, **kw: _failing_proc(cmd, lines))
    with pytest.raises(RuntimeError) as info:
        ha.run_agent("go", allow_gh=False, cwd="/tmp")
    assert not isinstance(info.value, ha.AgentUnavailable)


def test_run_agent_raises_agent_unavailable_when_the_cli_is_missing(monkeypatch):
    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(ha.subprocess, "Popen", missing)
    with pytest.raises(ha.AgentUnavailable, match="not installed"):
        ha.run_agent("go", allow_gh=False, cwd="/tmp")


def test_probe_reports_the_outage_and_none_when_healthy(monkeypatch):
    monkeypatch.setattr(ha.subprocess, "Popen",
                        lambda cmd, **kw: _failing_proc(cmd, ["Not logged in. Please run /login"]))
    assert "Not logged in" in (ha.probe() or "")
    monkeypatch.setattr(ha.subprocess, "Popen", lambda cmd, **kw: _FakeProc(cmd))
    assert ha.probe() is None


class _FailingProc(_FakeProc):
    """A CLI run that exits non-zero after printing `lines` outside the stream."""

    def __init__(self, cmd, lines: list[str], code: int = 1):
        super().__init__(cmd)
        self.returncode = code
        self.stdout = iter(lines)


def _popen_with(factory):
    def fake_popen(cmd, **kwargs):
        return factory(cmd)
    return fake_popen


def test_a_prompt_the_safeguards_refuse_raises_agent_declined(monkeypatch):
    monkeypatch.setattr(ha.subprocess, "Popen", _popen_with(lambda cmd: _FailingProc(cmd, [
        "API Error: Opus 5 (1M context)'s safeguards flagged this message "
        "(https://www.anthropic.com/legal/aup). Try rephrasing."])))
    with pytest.raises(ha.AgentDeclined, match="safeguards flagged"):
        ha.run_agent("describe this PR", allow_gh=False, cwd="/tmp")


def test_an_expired_login_still_reads_as_unavailable_not_declined(monkeypatch):
    monkeypatch.setattr(ha.subprocess, "Popen", _popen_with(
        lambda cmd: _FailingProc(cmd, ["OAuth token has expired. Please run /login"])))
    with pytest.raises(ha.AgentUnavailable):
        ha.run_agent("x", allow_gh=False, cwd="/tmp")


def test_every_run_is_pinned_to_the_configured_model(monkeypatch):
    procs: list[_FakeProc] = []
    monkeypatch.setattr(ha.subprocess, "Popen", _popen_with(lambda cmd: procs.append(_FakeProc(cmd)) or procs[-1]))
    monkeypatch.delenv("TRIAGE_AGENT_MODEL", raising=False)
    ha.run_agent("x", allow_gh=False, cwd="/tmp")
    assert procs[-1].cmd[procs[-1].cmd.index("--model") + 1] == "opus"
    monkeypatch.setenv("TRIAGE_AGENT_MODEL", "sonnet")
    ha.run_agent("x", allow_gh=False, cwd="/tmp")
    assert procs[-1].cmd[procs[-1].cmd.index("--model") + 1] == "sonnet"
    ha.run_agent("x", allow_gh=False, cwd="/tmp", model="haiku")
    assert procs[-1].cmd[procs[-1].cmd.index("--model") + 1] == "haiku"
    monkeypatch.setenv("TRIAGE_AGENT_MODEL", "")
    ha.run_agent("x", allow_gh=False, cwd="/tmp")
    assert "--model" not in procs[-1].cmd


def test_json_reply_runs_once_more_when_the_first_answer_is_cut_off():
    answers = iter(['{"changes": [{"path": "a.ts", "rationale": "unterminat',
                    '{"changes": []}'])
    calls = 0

    def run() -> str:
        nonlocal calls
        calls += 1
        return next(answers)

    verdict, text = ha.json_reply(run)
    assert verdict == {"changes": []} and text == '{"changes": []}' and calls == 2


def test_json_reply_raises_after_a_second_cut_off_answer():
    with pytest.raises(ValueError):
        ha.json_reply(lambda: "no json here")


_TAIL = ["--permission-mode", "dontAsk", "--safe-mode", "--setting-sources", ""]
_GH_RULES = ("Bash(gh pr view:*),Bash(gh pr diff:*),Bash(gh pr list:*),"
             "Bash(gh pr checks:*),Bash(gh issue view:*),Bash(gh issue list:*),"
             "Bash(gh search prs:*),Bash(gh search issues:*),"
             f"Bash({ha.GH_READ}:*)")
_DENIED = ["Task", "Edit", "Write", "NotebookEdit", "EnterPlanMode", "ExitPlanMode",
           "EnterWorktree", "ExitWorktree", "Skill", "Workflow", "SendMessage",
           "WebFetch", "WebSearch", "AskUserQuestion"]


def test_flags_without_read_root_are_the_unscoped_list_verbatim():
    assert ha._flags(False) == [
        "--allowedTools", "Read,Grep,Glob", "--disallowedTools", *_DENIED, *_TAIL]
    assert ha._flags(True) == [
        "--allowedTools", f"Read,Grep,Glob,{_GH_RULES},Bash(git log:*)",
        "--disallowedTools", *_DENIED, *_TAIL]
    assert ha._flags(False, allow=["Bash(/x/tool:*)"]) == [
        "--allowedTools", "Read,Grep,Glob,Bash(/x/tool:*)",
        "--disallowedTools", *_DENIED, *_TAIL]


def _allowed(flags: list[str]) -> list[str]:
    return flags[flags.index("--allowedTools") + 1].split(",")


def test_read_root_replaces_the_bare_read_tools_with_rules_over_the_resolved_root(tmp_path):
    real = tmp_path / "wt"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    root = f"/{real.resolve()}"
    assert _allowed(ha._flags(False, read_root=f"{link}/")) == [
        f"Read({root}/**)", f"Grep({root}/**)", f"Glob({root}/**)"]


def test_a_read_root_naming_a_file_grants_that_file_alone(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    patch = tmp_path / "patches" / "abc.patch"
    patch.parent.mkdir()
    patch.write_text("diff --git a/x b/x\n")
    allowed = _allowed(ha._flags(False, read_root=[str(wt), str(patch)]))
    root, file = f"/{wt.resolve()}", f"/{patch.resolve()}"
    assert allowed == [f"Read({root}/**)", f"Grep({root}/**)", f"Glob({root}/**)",
                       f"Read({file})", f"Grep({file})"]


def test_a_scoped_agent_gets_gh_without_any_git_prefix_rule(tmp_path):
    allowed = _allowed(ha._flags(True, read_root=str(tmp_path)))
    assert "Bash(gh pr view:*)" in allowed and f"Bash({ha.GH_READ}:*)" in allowed
    assert not any(a.startswith("Bash(git ") for a in allowed)


def test_git_root_grants_the_pinned_reader_and_no_git_prefix_rule(tmp_path):
    allowed = _allowed(ha._flags(True, edit_root=str(tmp_path), read_root=str(tmp_path),
                                 git_root=str(tmp_path)))
    assert f"Bash({ha.GIT_READ}:*)" in allowed
    assert not any(a.startswith("Bash(git ") for a in allowed)


def test_an_edit_root_alone_grants_no_git(tmp_path):
    allowed = _allowed(ha._flags(False, edit_root=str(tmp_path), read_root=str(tmp_path)))
    assert not any("git" in a for a in allowed)


@pytest.mark.parametrize("kwargs", [{"edit_root": "/wt"}, {"git_root": "/wt"}])
def test_edit_and_git_grants_are_refused_without_a_read_root(kwargs):
    with pytest.raises(ValueError, match="read_root"):
        ha._flags(False, **kwargs)


def _capture_popen(monkeypatch) -> dict:
    seen: dict = {}

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs, cmd=cmd)
        return _FakeProc(cmd)

    monkeypatch.setattr(ha.subprocess, "Popen", fake_popen)
    return seen


def test_a_scoped_run_refuses_a_cwd_outside_its_directory_roots(monkeypatch, tmp_path):
    # The CLI reads its working directory whatever the rules say.
    seen = _capture_popen(monkeypatch)
    root = tmp_path / "root"
    root.mkdir()
    patch = tmp_path / "other" / "pr.patch"
    patch.parent.mkdir()
    patch.write_text("x")
    for cwd in (tmp_path, patch.parent):
        with pytest.raises(ValueError, match="cwd"):
            ha.run_agent("go", allow_gh=False, cwd=str(cwd),
                         read_root=[str(root), str(patch)])
    assert not seen
    sub = root / "pkg"
    sub.mkdir()
    assert ha.run_agent("go", allow_gh=False, cwd=str(sub), read_root=str(root)) == "ok"


@pytest.mark.parametrize("grant", ["edit_root", "git_root"])
def test_a_scoped_run_refuses_an_edit_or_git_root_outside_its_read_roots(
        monkeypatch, tmp_path, grant):
    _capture_popen(monkeypatch)
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError, match=grant):
        ha.run_agent("go", allow_gh=False, cwd=str(root), read_root=str(root),
                     **{grant: str(tmp_path)})


def test_git_root_pins_the_reader_to_the_resolved_worktree(monkeypatch, tmp_path):
    seen = _capture_popen(monkeypatch)
    real = tmp_path / "wt"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    ha.run_agent("go", allow_gh=False, cwd=str(real), read_root=str(link),
                 git_root=str(link))
    assert seen["env"][ha.GIT_READ_ENV] == str(real.resolve())


def test_env_allow_keeps_the_clis_needs_the_named_variables_and_env_extra(monkeypatch):
    seen = _capture_popen(monkeypatch)
    for k, v in {"TRIAGE_STORE_URL": "postgres://secret", "SSH_AUTH_SOCK": "/s",
                 "AWS_SECRET_ACCESS_KEY": "k", "DOCKER_HOST": "unix:///d.sock",
                 "ANTHROPIC_API_KEY": "sk", "CLAUDE_CONFIG_DIR": "/c", "LC_ALL": "C",
                 "HOME": "/home/op", "PATH": "/bin"}.items():
        monkeypatch.setenv(k, v)
    ha.run_agent("go", allow_gh=False, cwd="/tmp", env_allow=["DOCKER_HOST"],
                 env_extra={"PROSPECTOR_CHECK_PR": "7"})
    env = seen["env"]
    assert {"DOCKER_HOST", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR", "LC_ALL", "HOME",
            "PATH", "PROSPECTOR_CHECK_PR"} <= set(env)
    assert not {"TRIAGE_STORE_URL", "SSH_AUTH_SOCK", "AWS_SECRET_ACCESS_KEY"} & set(env)
    assert all(k in ha._CLI_ENV or k.startswith(ha._CLI_ENV_PREFIXES)
               or k in ("DOCKER_HOST", "PROSPECTOR_CHECK_PR") for k in env)


def test_without_env_allow_the_agent_inherits_the_operator_environment(monkeypatch):
    seen = _capture_popen(monkeypatch)
    monkeypatch.setenv("TRIAGE_STORE_URL", "postgres://secret")
    ha.run_agent("go", allow_gh=False, cwd="/tmp")
    assert seen["env"]["TRIAGE_STORE_URL"] == "postgres://secret"


def test_workdir_is_a_resolved_private_directory_that_lives_for_the_block():
    with ha.workdir("agent-test-") as tmp:
        assert os.path.isdir(tmp) and os.path.realpath(tmp) == tmp
        assert os.path.basename(tmp).startswith("agent-test-")
    assert not os.path.exists(tmp)


def test_probe_runs_scoped_to_an_empty_directory_under_the_env_allowlist(monkeypatch):
    seen = _capture_popen(monkeypatch)
    monkeypatch.setenv("TRIAGE_STORE_URL", "postgres://secret")
    assert ha.probe() is None
    allowed = _allowed(seen["cmd"])
    root = f"/{seen['cwd']}"
    assert allowed == [f"Read({root}/**)", f"Grep({root}/**)", f"Glob({root}/**)"]
    assert "TRIAGE_STORE_URL" not in seen["env"]


def test_run_on_bundle_runs_the_agent_in_a_private_directory_holding_the_bundle(
        monkeypatch, tmp_path):
    seen: dict = {}

    def fake_run(prompt, **kw):
        path = prompt.removeprefix("read ")
        seen.update(kw, path=path, bundle=json.load(open(path)))
        return "ok"

    monkeypatch.setattr(ha, "run_agent", fake_run)
    extra = tmp_path / "abc.diff"
    extra.write_text("d")
    out = ha.run_on_bundle([{"n": 1}], lambda p: f"read {p}", prefix="wave-",
                           allow_gh=True, read_root=[str(extra)])
    assert out == "ok" and seen["bundle"] == [{"n": 1}]
    assert os.path.dirname(seen["path"]) == seen["cwd"] == os.path.realpath(seen["cwd"])
    assert seen["read_root"] == [seen["cwd"], str(extra)]
    assert seen["allow_gh"] is True and list(seen["env_allow"]) == []
    assert not os.path.exists(seen["cwd"])
