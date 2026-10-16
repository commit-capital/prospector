"""Codex runs under the boundary Claude runs get: the generated permissions
profile and execpolicy, the run's private Codex home and scrubbed environment,
and — where the Codex CLI is installed — the profile checked against the real
sandbox, which spends no model calls."""
from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from pipeline import headless_agent, headless_codex

TOOL = str(headless_agent.REPO_ROOT / "prospector_app" / "agent" / "issue-sandbox-check")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake operator home with a Codex login, a clone under it, and a key
    file the deployment names."""
    user = tmp_path / "user"
    (user / ".codex").mkdir(parents=True)
    (user / ".codex" / "auth.json").write_text("{}")
    (user / "keys").mkdir()
    (user / "keys" / "bot.pem").write_text("KEY")
    clone = user / "scratch" / "clone"
    clone.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("TRIAGE_BOT_KEY_FILE", str(user / "keys" / "bot.pem"))
    return {"user": user, "clone": clone}


def test_the_profile_denies_the_home_and_credentials_but_reaches_the_roots(home, tmp_path):
    diff = home["user"] / "scratch" / "pr.diff"
    fs = headless_codex.filesystem([str(home["clone"]), str(diff)], str(home["clone"]),
                                   str(tmp_path / "t"), str(tmp_path / "h"))
    real = os.path.realpath
    assert fs["/"] == "read" and fs[real(home["user"])] == "none"
    assert fs[real(home["clone"])] == "write" and fs[real(diff)] == "read"
    assert fs[real(home["user"] / "keys" / "bot.pem")] == "none"
    assert fs[real(headless_agent.REPO_ROOT / ".env")] == "none"
    assert fs[real(home["user"] / ".codex")] == "none"
    assert fs[real(tmp_path / "h")] == "none" and fs[real(tmp_path / "t")] == "write"


def test_only_the_allowed_tool_leaves_the_sandbox_and_code_runners_are_forbidden():
    text = headless_codex.rules(headless_codex.allowed_commands([f"Bash({TOOL}:*)"]))
    allows = [line for line in text.splitlines() if '"allow"' in line]
    assert allows == [f'prefix_rule(pattern = {json.dumps([os.path.realpath(TOOL)])}, '
                      'decision = "allow")']
    for cmd in ("node", "pnpm", "python3", "git", "docker", "curl"):
        assert f'["{cmd}"], decision = "forbidden"' in text


@pytest.mark.parametrize("rule", ["Bash(git:*)", "Read(/etc/**)", "Bash(/no/such/tool:*)"])
def test_a_rule_with_no_codex_counterpart_is_refused(rule):
    with pytest.raises(ValueError):
        headless_codex.allowed_commands([rule])


@pytest.mark.parametrize("kw", [{"allow_gh": True}, {"git_root": "/x"}, {"read_root": None}])
def test_a_run_that_would_reach_github_git_or_everything_is_refused(home, kw):
    args = {"model": None, "cwd": str(home["clone"]), "read_root": str(home["clone"]),
            "edit_root": None, "allow": (), "env_allow": (), "env_extra": None,
            "timeout": 5, "allow_gh": False, "git_root": None, "system_prompt": None, **kw}
    with pytest.raises(ValueError):
        headless_codex.run("p", **args)


def test_the_note_lands_ahead_of_the_output_section():
    out = headless_codex.with_context("# Behavior\nfix it\n\n# Output\nReturn JSON.")
    assert out.index("Working in Codex") < out.index("# Output") and out.endswith("Return JSON.")


def test_the_stream_s_last_message_usage_and_failure_are_read():
    lines = [json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                                                            "text": "first"}}),
             "not json",
             json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                                                            "text": "last"}}),
             json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}}),
             json.dumps({"type": "error", "message": "boom"})]
    assert headless_codex.parse(lines) == ("last", {"input_tokens": 5}, "boom")


def test_run_agent_sends_a_codex_model_to_codex(monkeypatch):
    seen = {}
    monkeypatch.setattr(headless_codex, "run", lambda prompt, **kw: seen.update(kw) or "ok")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("claude spawned"))
    assert headless_agent.run_agent("p", allow_gh=False, cwd="/", model="codex:gpt-x",
                                    read_root="/") == "ok"
    assert seen["model"] == "gpt-x"


def _fake_codex(tmp_path: Path, stream: list[dict], exit_code: int = 0) -> Path:
    """A `codex` that records its argv, environment and Codex home, then
    prints `stream`."""
    record = tmp_path / "record.json"
    script = tmp_path / "codex"
    lines = "\n".join(json.dumps(e) for e in stream)
    script.write_text(f"""#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
home = os.environ["CODEX_HOME"]
json.dump({{"argv": sys.argv[1:], "env": dict(os.environ),
           "auth": os.path.realpath(os.path.join(home, "auth.json")),
           "rules": open(os.path.join(home, "rules", "default.rules")).read()}},
          open({str(record)!r}, "w"))
print({lines!r})
sys.exit({exit_code})
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return record


def _run(home, tmp_path, monkeypatch, stream, exit_code=0):
    record = _fake_codex(tmp_path, stream, exit_code)
    monkeypatch.setattr(headless_codex, "CODEX_BIN", str(tmp_path / "codex"))
    booked: list[dict] = []
    monkeypatch.setattr(headless_agent, "_store",
                        lambda: type("S", (), {"append_agent_run": lambda self, r:
                                               booked.append(r)})())
    monkeypatch.setenv("TRIAGE_STORE_URL", "postgresql://secret")
    out = None
    try:
        out = headless_codex.run(
            "fix it", model="gpt-x", cwd=str(home["clone"]), read_root=str(home["clone"]),
            edit_root=str(home["clone"]), allow=[f"Bash({TOOL}:*)"],
            env_allow=(), env_extra={"PROSPECTOR_ISSUE_CHECK_ISSUE": "7"}, timeout=30,
            allow_gh=False, git_root=None, system_prompt=None)
    finally:
        seen = json.loads(record.read_text()) if record.exists() else None
    return out, seen, booked


DONE = [{"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": 1}'}},
        {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}}]


def test_a_run_gets_a_private_home_holding_the_login_and_the_rules(home, tmp_path, monkeypatch):
    out, seen, booked = _run(home, tmp_path, monkeypatch, DONE)
    assert out == '{"ok": 1}'
    assert seen["auth"] == os.path.realpath(home["user"] / ".codex" / "auth.json")
    assert os.path.realpath(seen["env"]["CODEX_HOME"]) != os.path.realpath(home["user"] / ".codex")
    assert '"allow"' in seen["rules"] and '"forbidden"' in seen["rules"]
    assert not Path(seen["env"]["CODEX_HOME"]).exists()
    assert booked[0]["account"] == "codex" and booked[0]["model"] == "codex:gpt-x"
    assert booked[0]["input_tokens"] == 3


def test_a_run_s_environment_is_scrubbed_and_its_config_ignored(home, tmp_path, monkeypatch):
    _, seen, _ = _run(home, tmp_path, monkeypatch, DONE)
    assert "TRIAGE_STORE_URL" not in seen["env"]
    assert seen["env"]["PROSPECTOR_ISSUE_CHECK_ISSUE"] == "7"
    argv = seen["argv"]
    for flag in ("--ignore-user-config", "--strict-config", "--json"):
        assert flag in argv
    assert 'web_search="disabled"' in argv and 'approval_policy="never"' in argv
    assert argv[argv.index("-m") + 1] == "gpt-x"


@pytest.mark.parametrize("message,error", [
    ("You've hit your usage limit", headless_agent.CapacityExhausted),
    ("Not logged in", headless_agent.AgentUnavailable),
    ("something broke", RuntimeError)])
def test_a_failed_run_raises_what_a_claude_run_would(home, tmp_path, monkeypatch, message, error):
    with pytest.raises(error):
        _run(home, tmp_path, monkeypatch, [{"type": "turn.failed",
                                            "error": {"message": message}}], exit_code=1)


def test_a_missing_login_is_the_machine_s_condition(home, tmp_path, monkeypatch):
    (home["user"] / ".codex" / "auth.json").unlink()
    with pytest.raises(headless_agent.AgentUnavailable):
        _run(home, tmp_path, monkeypatch, DONE)


# The generated profile against the real sandbox: no model call is made.
needs_codex = pytest.mark.skipif(shutil.which("codex") is None or os.uname().sysname != "Darwin",
                                 reason="needs the Codex CLI's macOS sandbox")


def _sandboxed(fs: dict[str, str], cwd: Path, *cmd: str) -> subprocess.CompletedProcess:
    table = headless_codex._toml_table(fs)
    return subprocess.run(
        ["codex", "sandbox", "--permissions-profile", "lane", "-c",
         f"permissions.lane.filesystem={table}", "-C", str(cwd), "--", *cmd],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "CODEX_HOME": str(cwd.parent / "codex-home")})


def _answers(path: str) -> bool:
    """Whether a Docker socket at `path` accepts a connection outside the sandbox."""
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(os.path.realpath(path))
        return True
    except OSError:
        return False


@needs_codex
def test_the_real_sandbox_holds_the_profile_s_walls(tmp_path):
    real_home = Path(os.path.expanduser("~")).resolve()
    scratch = Path(os.path.realpath(tmp_path))
    clone = real_home / ".prospector-codex-wall-test" / scratch.name / "clone"
    clone.mkdir(parents=True)
    (clone.parent / "codex-home").mkdir()
    (clone / "a.txt").write_text("ok")
    (clone / ".env").write_text("SECRET")
    (clone.parent / "sibling.txt").write_text("other")
    try:
        fs = {**headless_codex.filesystem([str(clone)], str(clone), str(scratch),
                                          str(clone.parent / "codex-home")),
              str(clone / ".env"): "none"}

        def ok(*cmd: str) -> bool:
            return _sandboxed(fs, clone, *cmd).returncode == 0

        assert ok("cat", "a.txt") and ok("sh", "-c", "echo x > b.txt")
        assert not ok("cat", ".env")
        assert not ok("cat", str(clone.parent / "sibling.txt"))
        assert not ok("ls", str(real_home))
        assert not ok("sh", "-c", f"echo x > {real_home}/.prospector-codex-pwned")
        assert not (real_home / ".prospector-codex-pwned").exists()
        assert not ok("curl", "-sS", "-m", "5", "https://example.com", "-o", "/dev/null")
        sock = next((s for s in ("/var/run/docker.sock",
                                 str(real_home / ".colima/default/docker.sock"))
                     if _answers(s)), None)
        if sock:
            probe = (f"import socket; s = socket.socket(socket.AF_UNIX); "
                     f"s.connect({os.path.realpath(sock)!r})")
            assert not ok("/usr/bin/python3", "-c", probe)
    finally:
        shutil.rmtree(clone.parent.parent, ignore_errors=True)
