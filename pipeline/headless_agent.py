"""Run a locked-down headless `claude -p` for the pipeline's single-cluster
re-run, and turn its stream-json output into (a) a final text result and
(b) live progress events.

Lockdown mirrors prospector_app/backend/chat.py: --safe-mode (drop the repo's
CLAUDE.md / hooks / plugins / skills / MCP) + --setting-sources "" (load no
settings file, so no repo grant or deny reaches this agent) + --permission-mode
dontAsk (never prompt, silently deny anything off the allowlist) + a read-only
toolset. Unlike the chat agent we do NOT grant `gh issue create`; ANALYZE only
needs read-only gh to cite already-landed upstream fixes. An opt-in `edit_root`
grants Edit/Write scoped to a single worktree plus read-only git, for the
conflict resolver and the fix author; the rule names the worktree in the CLI's
`//absolute` form, since a single leading slash is project-root-relative.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
import os
import re
import shutil
import signal
import subprocess
import threading

from pipeline import settings
from pipeline.gh import operator_env
from pipeline.settings import REPO_ROOT

CLAUDE_BIN = shutil.which("claude") or "claude"


class AgentUnavailable(RuntimeError):
    """The CLI could not do any work at all: the binary is missing, or it
    exited because it is not authenticated. This machine's condition, never a
    verdict on the work — callers end such a run as retryable, and a worker
    trips its agent lanes until a probe passes."""


# What the CLI prints when it cannot serve any prompt: an expired or missing
# login, or a rejected key. Matched against the run's output on a non-zero exit.
_UNAVAILABLE = re.compile(
    r"Failed to authenticate|OAuth (?:access )?token (?:has )?expired|Not logged in"
    r"|Please run /login|Invalid API key|invalid_api_key|authentication_error"
    r"|API Error: 401", re.I)


def _failure_text(text: str, raw_lines: list[str], results: list[dict]) -> str:
    """Where a failed run's own complaint can be: the terminal result event's
    error text, the lines the CLI printed outside the stream, and the message
    text when the CLI marked the run an error or never sent a result. A run
    that completed with a result the CLI did not flag keeps its prose out of
    the scan, so an assistant quoting an error string is not an outage."""
    result = results[0] if results else None
    parts: list[str] = []
    if result is not None and result.get("is_error"):
        parts.append(str(result.get("result") or ""))
    parts.extend(raw_lines)
    if result is None or result.get("is_error"):
        parts.append(text)
    return "\n".join(parts)


def unavailable_reason(text: str) -> str | None:
    """The line of `text` that says the CLI cannot serve prompts, or None."""
    for line in text.splitlines():
        if _UNAVAILABLE.search(line):
            return line.strip()[:300]
    return None


class AgentDeclined(RuntimeError):
    """The model's safeguards refused the prompt itself. A verdict on this
    request's text, never the machine's condition: callers end such a run as
    refused, and it counts toward no lane trip."""


# What the CLI prints when the API's safeguards refuse a prompt outright.
_DECLINED = re.compile(r"safeguards flagged this message"
                       r"|can't respond to this message", re.I)


def declined_reason(text: str) -> str | None:
    """The line of `text` that says the model refused the prompt, or None."""
    for line in text.splitlines():
        if _DECLINED.search(line):
            return line.strip()[:300]
    return None


class EditsBlockedError(RuntimeError):
    """The CLI denied an Edit/Write on a path inside the granted edit_root.

    The grant covers the whole worktree subtree, so an in-root denial means the
    permission rule is not reaching the agent (a CLI whose rule syntax moved, a
    worktree path the rule form cannot name) — this machine's fault, never a
    verdict on the change. Callers end such a run as retryable."""


def fill(template: str, subs: dict[str, object]) -> str:
    """Substitute `__TOKEN__` placeholders in `template` in a SINGLE pass, so a
    substituted value that itself contains another token is never re-substituted
    (token order is irrelevant — safe even when a value is untrusted PR text). The
    canonical agent prompts are shipped as templates; consumers fill the per-call
    placeholders with this. Mirrors the `fill` helper in pipeline/workflows/*.js."""
    pattern = re.compile("|".join(re.escape(k) for k in subs))
    return pattern.sub(lambda m: str(subs[m.group(0)]), template)

# Read-only gh the analyst may use to find an already-landed upstream fix. Raw
# `gh api` is never allowlisted: a later `-X` overrides an earlier one, so no
# prefix rule can hold it to GET. Raw contents, code search, a path's commit
# history, and a single commit come through prospector_app/agent/gh-read, which
# fixes the method and builds the endpoint itself (the chat agent's window too).
GH_READ = str(REPO_ROOT / "prospector_app" / "agent" / "gh-read")
_GH_ALLOW = [
    "Bash(gh pr view:*)", "Bash(gh pr diff:*)", "Bash(gh pr list:*)",
    "Bash(gh pr checks:*)", "Bash(gh issue view:*)", "Bash(gh issue list:*)",
    "Bash(gh search prs:*)", "Bash(gh search issues:*)",
    f"Bash({GH_READ}:*)", "Bash(git log:*)",
]
_DISALLOWED = [
    "Task", "Edit", "Write", "NotebookEdit",
    "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
    "Skill", "Workflow", "SendMessage",
    "WebFetch", "WebSearch", "AskUserQuestion",
]


# Read-only git the conflict resolver may run inside its worktree to see both
# sides of a conflict. No push, no commit — the resubmit tool owns every git
# write.
_GIT_READ_ALLOW = [
    "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git status:*)",
]


def _flags(allow_gh: bool, edit_root: str | None = None,
           allow: Sequence[str] = ()) -> list[str]:
    tools = ["Read", "Grep", "Glob", *(_GH_ALLOW if allow_gh else []), *allow]
    disallowed = list(_DISALLOWED)
    if edit_root:
        # A rule path beginning with one slash resolves against the project
        # root; "//" is the CLI's filesystem-absolute form. The CLI matches it
        # against the path the agent passes, and a rule naming a symlink never
        # matches, so the rule names the resolved root and the caller's worktree
        # path must itself be free of symlinks. Under dontAsk a rule that fails
        # to match is a silent denial of every edit.
        root = "/" + os.path.realpath(edit_root).rstrip("/")
        tools += [f"Edit({root}/**)", f"Write({root}/**)", *_GIT_READ_ALLOW]
        disallowed = [t for t in disallowed if t not in ("Edit", "Write")]
    return [
        "--allowedTools", ",".join(tools),
        "--disallowedTools", *disallowed,
        "--permission-mode", "dontAsk",
        "--safe-mode",
        "--setting-sources", "",
    ]


def parse_stream(lines, on_event=None, on_result=None, on_raw=None) -> str:
    """Consume claude stream-json lines; return the concatenated assistant text.
    Calls on_event((kind, name, input)) for tool uses so callers can show
    progress: ("tool", tool_name, tool_input_dict), on_result(event) with
    the CLI's terminal result event, which carries the run's permission
    denials, and on_raw(line) with every non-JSON line — the CLI's own
    complaints, which arrive outside the stream."""
    parts: list[str] = []
    saw_delta = False
    for raw in lines:
        s = raw.strip() if isinstance(raw, str) else raw.decode("utf-8", "replace").strip()
        if not s:
            continue
        try:
            e = json.loads(s)
        except json.JSONDecodeError:
            if on_raw:
                on_raw(s)
            continue
        t = e.get("type")
        if t == "stream_event":
            ev = e.get("event", {})
            if ev.get("type") == "content_block_delta":
                d = ev.get("delta", {})
                if d.get("type") == "text_delta" and d.get("text"):
                    parts.append(d["text"])
                    saw_delta = True
        elif t == "assistant":
            for c in e.get("message", {}).get("content", []):
                if c.get("type") == "tool_use" and on_event:
                    on_event(("tool", c.get("name", "?"), c.get("input") or {}))
                if c.get("type") == "text" and c.get("text") and not saw_delta:
                    parts.append(c["text"])
        elif t == "result":
            if on_result:
                on_result(e)
            break
    return "".join(parts)


def _blocked_edits(result: dict | None, edit_root: str) -> list[str]:
    """The file paths of Edit/Write permission denials inside `edit_root`, read
    from the CLI's terminal result event. The grant covers the whole subtree,
    so any entry here means the grant is not working."""
    root = os.path.realpath(edit_root).rstrip("/")
    blocked: list[str] = []
    for d in (result or {}).get("permission_denials") or []:
        if d.get("tool_name") not in ("Edit", "Write"):
            continue
        path = str((d.get("tool_input") or {}).get("file_path") or "")
        if path and os.path.realpath(path).startswith(root + "/"):
            blocked.append(path)
    return blocked


def tool_summary(name: str, inp: dict, width: int = 100) -> str:
    """One-line, screen-fitting description of a tool use — the salient input
    (Bash command, Read file, Glob/Grep pattern) after the tool name, with
    whitespace collapsed and truncated to `width`."""
    inp = inp or {}
    if name == "Bash":
        detail = inp.get("command", "")
    elif name == "Read":
        detail = inp.get("file_path", "")
    elif name in ("Glob", "Grep"):
        detail = inp.get("pattern", "")
        if inp.get("path"):
            detail += f" in {inp['path']}"
    else:
        detail = next((v for v in inp.values() if isinstance(v, str)), "")
    detail = " ".join(str(detail).split())
    line = f"{name}: {detail}" if detail else name
    return line[:width - 1] + "…" if len(line) > width else line


def progress_line(ev) -> str | None:
    """The one-line progress string for a tool-use event, or None for events that
    don't render (so callers can print it live or buffer it identically)."""
    if ev and ev[0] == "tool":
        inp = ev[2] if len(ev) > 2 else {}
        return f"    · {tool_summary(ev[1], inp)}"
    return None


def print_progress(ev) -> None:
    """An `on_event` callback for run_agent that prints each tool use as an
    indented one-line progress entry to stdout."""
    line = progress_line(ev)
    if line is not None:
        print(line, flush=True)


def extract_json(text: str) -> dict:
    """Pull the JSON object out of an agent's free-text answer: prefer a
    ```json fenced block, else the last balanced {...} run. A raw newline or
    tab inside a string is accepted, since an agent writing prose into a
    field breaks lines there. Raises ValueError."""
    import re
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1), strict=False)
    # last balanced object
    depth = 0
    start = None
    candidate = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start:i + 1]
    if candidate is None:
        raise ValueError("no JSON object found in agent output")
    return json.loads(candidate, strict=False)


def json_reply(run: Callable[[], str]) -> tuple[dict, str]:
    """`run` the agent and parse the JSON object out of its answer, running it
    once more when the first answer carries no parseable object — a reply cut
    off mid-string is the run's accident, not the prompt's verdict. Returns
    the object with the text it came from; the second failure raises."""
    text = run()
    try:
        return extract_json(text), text
    except ValueError:
        text = run()
        return extract_json(text), text


def run_agent(prompt: str, *, allow_gh: bool, cwd: str, system_prompt: str | None = None,
              model: str | None = None, on_event=None, timeout: int = 1200,
              edit_root: str | None = None, allow: Sequence[str] = (),
              env_extra: Mapping[str, str] | None = None) -> str:
    """Spawn headless claude, stream its output through parse_stream, return the
    final text. The prompt travels over stdin — it can embed a whole PR diff,
    and argv has an OS size cap. `model` pins a specific model (e.g. a cheap
    Haiku for mechanical work); None takes `settings.agent_model()`, and only
    an empty TRIAGE_AGENT_MODEL leaves the CLI's own default in charge.
    `edit_root` grants
    Edit/Write scoped to that directory (plus read-only git). `allow` adds
    permission rules on top of the read-only set, such as `Bash(<tool>:*)` for
    one more host command; `env_extra` is merged into the agent's environment,
    which its Bash commands inherit. Raises RuntimeError on a non-zero exit,
    and EditsBlockedError when an Edit/Write inside `edit_root` was
    permission-denied — the grant not working, so the run's outcome is the
    machine's, not the agent's. Raises AgentDeclined when the API's safeguards
    refused the prompt, and AgentUnavailable when the CLI could serve none."""
    cmd = [CLAUDE_BIN, "-p", *_flags(allow_gh, edit_root, allow),
           "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    model = model or settings.agent_model()
    if model:
        cmd += ["--model", model]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    env = operator_env()
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                start_new_session=True, env=env)
    except FileNotFoundError:
        raise AgentUnavailable(f"{CLAUDE_BIN} is not installed or not on PATH")
    assert proc.stdin is not None and proc.stdout is not None

    # Fed from a thread while this thread drains stdout, so neither pipe can
    # fill and deadlock the pair.
    def _feed(stdin=proc.stdin) -> None:
        try:
            stdin.write(prompt)
        except BrokenPipeError:
            pass
        finally:
            stdin.close()

    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()
    results: list[dict] = []
    raw_lines: list[str] = []
    text = parse_stream(proc.stdout, on_event=on_event, on_result=results.append,
                        on_raw=raw_lines.append)
    feeder.join(timeout=60)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        raise RuntimeError(f"claude did not exit within {timeout}s")
    if proc.returncode != 0:
        tail = text[-500:] if text else "(no output)"
        failure = _failure_text(text, raw_lines, results)
        why = unavailable_reason(failure)
        if why:
            raise AgentUnavailable(f"claude exited {proc.returncode}: {why}")
        declined = declined_reason(failure)
        if declined:
            raise AgentDeclined(declined)
        raise RuntimeError(f"claude exited {proc.returncode}; last output: {tail}")
    if edit_root:
        blocked = _blocked_edits(results[0] if results else None, edit_root)
        if blocked:
            raise EditsBlockedError(
                f"the edit grant did not reach the agent: {len(blocked)} "
                f"Edit/Write call(s) inside {edit_root} were permission-denied "
                f"(first: {blocked[0]})")
    return text


def probe(timeout: int = 180) -> str | None:
    """Whether the CLI can serve a prompt on this machine right now: one
    trivial headless run on the cheapest model. None when it answered, else
    the reason it could not — what a tripped agent lane retests with."""
    try:
        run_agent("Reply with the single word ok.", allow_gh=False,
                  cwd=str(REPO_ROOT), model="haiku", timeout=timeout)
    except RuntimeError as e:
        return str(e)
    return None
