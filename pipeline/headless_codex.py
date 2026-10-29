"""Headless Codex runs, held to the boundary `headless_agent.run_agent` gives
Claude: a model named `codex:<model>` runs here.

Every shell command the agent runs is confined by a Codex permissions profile
(macOS seatbelt): it reads the system directories and its read roots, writes
its edit root and a private temp directory, and reaches nothing under the
operator's home, no credential the deployment names, and no network. Escalation
is off, so no command leaves the profile; the runners that would execute the
repository's code are forbidden outright. A host tool an `allow` rule names
(`Bash(<tool>:*)`, the sandbox check) is run by the host for the agent: the
prompt names a stand-in inside the profile that files its arguments in a spool
directory, and `_Broker` runs the tool with them as an argument list and files
back its output and exit, which is the reach `Bash(<tool>:*)` grants Claude.
Each run gets its own Codex home holding the operator's login and the rules
and nothing else, and starts from no user configuration, no repository
instruction files, and the environment `headless_agent` scrubs for Claude.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from pipeline import capacity, headless_agent, settings

CODEX_BIN = shutil.which("codex") or "codex"
PREFIX = "codex:"
ACCOUNT = "codex"

_PROFILE = "lane"

# The runners that would execute the repository's code on the host, and the
# tools that reach past the worktree. The profile confines them regardless;
# forbidding them sends the agent to the sandbox check instead.
_FORBIDDEN = ("node", "npx", "npm", "pnpm", "yarn", "bun", "deno", "tsx", "ts-node",
              "vitest", "jest", "python", "python3", "uv", "pip", "make", "docker",
              "git", "gh", "curl", "wget", "ssh", "scp")

_KEY_FILE_VARS = ("TRIAGE_BOT_KEY_FILE", "TRIAGE_PUSH_SSH_KEY_FILE")

_ALLOW_RE = re.compile(r"^Bash\((.+):\*\)$")

# What a brokered tool's output is cut to, from its end.
OUTPUT_BYTES = 200_000

CONTEXT = """

## Working in Codex

Your shell runs in a sandbox: it reads and writes the worktree, reads the system's tools, and has no network. Read and search files with ordinary commands (`rg`, `sed -n`, `cat`, `ls`) and edit them with apply_patch. Do not run the project's code yourself (node, pnpm, a test runner): the check command above is the only way to run it, and it runs in an isolated container. It can take minutes; wait for it.
"""

# The note for a run with no edit root and no host tool: a reviewer or judge.
READ_ONLY_CONTEXT = """

## Working in Codex

Your shell runs in a sandbox: it reads the worktree and the system's tools, writes only a private temp directory, and has no network. Read and search files with ordinary commands (`rg`, `sed -n`, `cat`, `ls`). Do not edit any file, and do not run the project's code (node, pnpm, a test runner).
"""

_OUTPUT_HEADING = "\n# Output\n"

# A stand-in for a host tool: it files its arguments, NUL-separated, and waits
# for the host's answer.
_STAND_IN = """#!/bin/sh
spool='__SPOOL__'
id="$$-$(date +%s)"
printf '%s\\0' "$@" > "$spool/$id.tmp" && mv "$spool/$id.tmp" "$spool/$id.req" || exit 1
while [ ! -f "$spool/$id.done" ]; do sleep 1; done
cat "$spool/$id.out"
exit "$(cat "$spool/$id.done")"
"""

_LIMIT = re.compile(r"usage limit|rate limit|hit your \w+ limit|quota", re.I)
_UNAVAILABLE = re.compile(r"not logged in|log ?in|unauthori[sz]ed|401|"
                          r"authentication|auth\.json|api key", re.I)


def _real(path: str | Path) -> str:
    return os.path.realpath(Path(path).expanduser())


def _secrets() -> list[str]:
    """The deployment's credentials as resolved paths: the repo-root .env and
    each configured private key."""
    paths = [_real(headless_agent.REPO_ROOT / ".env")]
    for var in _KEY_FILE_VARS:
        raw = os.environ.get(var, "").strip()
        if raw:
            paths.append(_real(raw))
    return paths


def filesystem(read_roots: Sequence[str], edit_root: str | None, tmp: str,
               codex_home: str, stand_ins: str | None = None) -> dict[str, str]:
    """The run's permissions profile, path to access. A more specific path's
    entry wins, so a root under the home it denies is still reachable."""
    fs = {"/": "read", _real(Path.home()): "none"}
    for root in read_roots:
        fs[_real(root)] = "read"
    if edit_root:
        fs[_real(edit_root)] = "write"
    fs[_real(tmp)] = "write"
    if stand_ins:
        fs[_real(stand_ins)] = "read"
    for path in [*_secrets(), _real(codex_home), _real(Path.home() / ".codex")]:
        fs[path] = "none"
    return fs


def rules() -> str:
    """The run's execpolicy: each forbidden command prefix never runs."""
    return "".join(f"prefix_rule(pattern = {json.dumps([cmd])}, decision = \"forbidden\")\n"
                   for cmd in _FORBIDDEN)


def allowed_commands(allow: Sequence[str]) -> list[str]:
    """The host tools `allow` rules name. A rule that is not `Bash(<tool>:*)`
    for a file is a ValueError: it has no Codex counterpart."""
    out = []
    for rule in allow:
        m = _ALLOW_RE.match(rule)
        if not m or not Path(m.group(1)).is_file():
            raise ValueError(f"{rule!r} has no Codex counterpart: only Bash(<tool>:*) "
                             "for a host tool is brokered")
        out.append(m.group(1))
    return out


def _config(key: str, value: object) -> list[str]:
    return ["-c", f"{key}={json.dumps(value)}"]


def _toml_table(table: Mapping[str, str]) -> str:
    return "{" + ", ".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in table.items()) + "}"


def flags(fs: Mapping[str, str], cwd: str, model: str | None) -> list[str]:
    return [
        "exec", "--json", "--ignore-user-config", "--strict-config",
        "--skip-git-repo-check", "-C", cwd,
        *_config("default_permissions", _PROFILE),
        "-c", f"permissions.{_PROFILE}.filesystem={_toml_table(fs)}",
        *_config("approval_policy", "never"),
        *_config("project_doc_max_bytes", 0),
        *_config("allow_login_shell", False),
        *_config("check_for_update_on_startup", False),
        *_config("web_search", "disabled"),
        *_config("features.multi_agent", False),
        *_config("features.apps", False),
        *_config("features.plugins", False),
        *_config("features.browser_use", False),
        *_config("features.standalone_web_search", False),
        *_config("shell_environment_policy.inherit", "all"),
        *(["-m", model] if model else []),
        "-",
    ]


def with_context(prompt: str, stand_ins: Mapping[str, str] | None = None, *,
                 read_only: bool = False) -> str:
    """`prompt` with each brokered tool's path replaced by its stand-in's and
    the Codex note — READ_ONLY_CONTEXT when `read_only` — ahead of its Output
    section, so the answer format stays last."""
    for tool, stand_in in (stand_ins or {}).items():
        prompt = prompt.replace(tool, stand_in)
    note = READ_ONLY_CONTEXT if read_only else CONTEXT
    behavior, heading, output = prompt.rpartition(_OUTPUT_HEADING)
    if not heading:
        return prompt + note
    return behavior + note + heading + output


def _prepare_home(home: Path) -> None:
    source = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    if not source.is_file():
        raise headless_agent.AgentUnavailable(
            "codex is not logged in on this machine (no file-backed auth.json)")
    (home / "auth.json").symlink_to(source.resolve())
    (home / "rules").mkdir()
    (home / "rules" / "default.rules").write_text(rules())


class _Broker:
    """Runs host tools for a sandboxed agent. Each tool gets a stand-in in
    `stand_ins` and a spool directory inside the agent's writable temp
    directory; a request is the arguments the stand-in filed, run as
    `[tool, *args]` under `env` in `cwd`, answered with the tool's output (its
    last OUTPUT_BYTES) and exit code. When the agent's run ends, a tool still
    running for it is stopped and left unanswered: nobody is waiting, and its
    spool is about to be removed. The tool is the sandbox check, whose phase
    container outlives it, so a stop sweeps the containers of dead checks."""

    def __init__(self, tools: Sequence[str], stand_ins: Path, tmp: Path, *,
                 env: Mapping[str, str], cwd: str) -> None:
        self.env, self.cwd = dict(env), cwd
        self.spools: dict[Path, str] = {}
        self.paths: dict[str, str] = {}
        for i, tool in enumerate(tools):
            spool = tmp / f"broker-{i}"
            spool.mkdir()
            stand_in = stand_ins / Path(tool).name
            stand_in.write_text(_STAND_IN.replace("__SPOOL__", str(spool)))
            stand_in.chmod(0o755)
            self.spools[spool] = tool
            self.paths[tool] = str(stand_in)
        self._stop = threading.Event()
        # Held to start a tool and to signal it, so a stop never misses one starting.
        self._lock = threading.Lock()
        self._running: subprocess.Popen[bytes] | None = None
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> _Broker:
        if self.spools:
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        with self._lock:
            self._stop.set()
            stopped = self._signal(signal.SIGTERM)
        if self._thread.is_alive():
            self._thread.join(timeout=10)
        if self._thread.is_alive():
            with self._lock:
                self._signal(signal.SIGKILL)
            self._thread.join(timeout=10)
        if stopped:
            from pipeline import verify_driver
            try:
                verify_driver.stop_orphaned_sandboxes()
            except Exception as e:
                print(f"[codex] removing a stopped check's sandbox containers failed: {e}",
                      flush=True)

    def _signal(self, sig: signal.Signals) -> bool:
        """Send `sig` to the running tool's process group; whether one ran."""
        proc = self._running
        if proc is None or proc.returncode is not None:
            return False
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.serve_pending():
                self._stop.wait(0.5)

    def serve_pending(self) -> int:
        """Answer every filed request, returning how many there were."""
        served = 0
        for spool, tool in self.spools.items():
            for req in sorted(spool.glob("*.req")):
                if self._stop.is_set():
                    return served
                name = req.name.removesuffix(".req")
                taken = spool / f"{name}.taken"
                try:
                    req.rename(taken)
                except OSError:
                    continue
                args = taken.read_bytes().split(b"\0")[:-1]
                self._answer(spool, name, tool, [a.decode("utf-8", "replace") for a in args])
                served += 1
        return served

    def _answer(self, spool: Path, name: str, tool: str, args: list[str]) -> None:
        try:
            with self._lock:
                if self._stop.is_set():
                    return
                self._running = proc = subprocess.Popen(
                    [tool, *args], cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            output = proc.communicate()[0][-OUTPUT_BYTES:]
            code = proc.returncode
        except OSError as e:
            output, code = f"the host could not run the check: {e}\n".encode(), 127
        finally:
            with self._lock:
                self._running = None
        if self._stop.is_set():
            return
        (spool / f"{name}.out").write_bytes(output)
        (spool / f"{name}.done").write_text(str(code))


def parse(lines: Sequence[str]) -> tuple[str, dict | None, str | None]:
    """The final agent message, the turn's usage, and a failure message from
    a `codex exec --json` event stream."""
    text, usage, failure = "", None, None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        item = event.get("item")
        if kind == "item.completed" and isinstance(item, dict) \
                and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            text = item["text"]
        elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
        elif kind in ("turn.failed", "error"):
            err = event.get("error") if kind == "turn.failed" else event
            failure = str((err or {}).get("message") or err) if isinstance(err, dict) else str(err)
    return text, usage, failure


def run(prompt: str, *, model: str | None, cwd: str, read_root: str | Sequence[str] | None,
        edit_root: str | None, allow: Sequence[str], env_allow: Sequence[str] | None,
        env_extra: Mapping[str, str] | None, timeout: int, allow_gh: bool,
        git_root: str | None, system_prompt: str | None) -> str:
    """Run `prompt` on Codex under the boundary the module describes and
    return its final message. Raises what `headless_agent.run_agent` raises
    for the same conditions."""
    if allow_gh or git_root:
        raise ValueError("a Codex run reaches neither GitHub nor git")
    if read_root is None:
        raise ValueError("a Codex run needs a read_root")
    roots = headless_agent._roots(read_root)
    headless_agent._assert_scoped(cwd, read_root, edit_root, None)
    tools = allowed_commands(allow)
    if system_prompt:
        prompt = system_prompt + "\n\n" + prompt
    lane = capacity.meter_lane()
    started = datetime.now(timezone.utc)
    env = headless_agent._agent_env(env_allow, env_extra)
    with tempfile.TemporaryDirectory(prefix="codex-home-") as home, \
            tempfile.TemporaryDirectory(prefix="codex-tmp-") as tmp, \
            tempfile.TemporaryDirectory(prefix="codex-bin-") as bin_dir, \
            _Broker(tools, Path(bin_dir), Path(tmp), env=env, cwd=cwd) as broker:
        _prepare_home(Path(home))
        fs = filesystem(roots, edit_root, tmp, home, bin_dir)
        cmd = [CODEX_BIN, *flags(fs, _real(cwd), model)]
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, start_new_session=True,
                                    env={**env, "CODEX_HOME": home, "TMPDIR": tmp})
        except FileNotFoundError:
            raise headless_agent.AgentUnavailable(f"{CODEX_BIN} is not installed or not on PATH")
        assert proc.stdin is not None and proc.stdout is not None

        def feed(stdin=proc.stdin) -> None:
            try:
                stdin.write(with_context(prompt, broker.paths,
                                         read_only=not edit_root and not tools))
            except BrokenPipeError:
                pass
            finally:
                stdin.close()

        threading.Thread(target=feed, daemon=True).start()
        lines: list[str] = []
        reader = threading.Thread(target=lambda: lines.extend(proc.stdout or []), daemon=True)
        reader.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            reader.join(timeout=10)
            _book(parse(lines)[1], lane, model, started)
            raise RuntimeError(f"codex did not exit within {timeout}s")
        reader.join(timeout=60)
    text, usage, failure = parse(lines)
    _book(usage, lane, model, started)
    if proc.returncode != 0 or failure:
        why = failure or " ".join("".join(lines[-20:]).split())[-500:] or "(no output)"
        if _LIMIT.search(why):
            raise headless_agent.CapacityExhausted(f"codex: usage limit reached: {why[:300]}",
                                                   None, None)
        if _UNAVAILABLE.search(why):
            raise headless_agent.AgentUnavailable(f"codex cannot serve prompts: {why[:300]}")
        raise RuntimeError(f"codex exited {proc.returncode}; last output: {why}")
    return text


def _book(usage: dict | None, lane: str | None, model: str | None,
          started: datetime) -> None:
    """Record a finished run's usage on the Codex account. Bookkeeping never
    fails the run it describes."""
    if not isinstance(usage, dict):
        return
    try:
        finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
        headless_agent._store().append_agent_run({
            "phase": "agent:run", "lane": lane or "operator", "account": ACCOUNT,
            "unattended": lane is not None, "model": f"{PREFIX}{model or ''}",
            "host": settings.worker_id(),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_input_tokens": usage.get("cached_input_tokens"),
            "cost_usd": None,
            "started": started.isoformat(timespec="seconds"), "finished": finished,
            "ts": finished})
    except Exception:
        return
