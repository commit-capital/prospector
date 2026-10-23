"""Where the evals' agents run: the local Claude CLI on a chosen model, with
usage bookkept in a throwaway store, or Codex in a read-only sandbox."""
from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from functools import partial
from pathlib import Path

from pipeline import headless_agent, settings, store


def codex_runner(binary: str, model: str | None) -> Callable[..., str]:
    """A `headless_agent.run_agent` stand-in that runs `codex exec` in a
    read-only sandbox, ephemeral and without the user's config, from `cwd`."""
    def run(prompt: str, *, cwd: str, timeout: int = 1200, **_: object) -> str:
        with tempfile.TemporaryDirectory(prefix="redteam-codex-") as tmp:
            last = Path(tmp) / "last.txt"
            argv = [binary, "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                    "--ephemeral", "--ignore-user-config", "--color", "never",
                    "-C", cwd, "-o", str(last), *(["-m", model] if model else []), "-"]
            done = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                                  timeout=timeout,
                                  env={"HOME": str(Path.home()),
                                       "PATH": os.defpath + ":/opt/homebrew/bin"})
            if done.returncode != 0 or not last.exists():
                raise RuntimeError(f"codex exited {done.returncode}: "
                                   f"{(done.stderr or done.stdout)[-400:]}")
            return last.read_text()
    return run


@contextmanager
def provider(name: str, *, codex_bin: str, model: str | None) -> Iterator[str]:
    """Every agent the defenses run goes to `name` while open; yields the
    model it records."""
    original = headless_agent.run_agent
    with ExitStack() as stack:
        stack.callback(setattr, headless_agent, "run_agent", original)
        if name == "claude":
            root = stack.enter_context(tempfile.TemporaryDirectory(prefix="redteam-usage-"))
            usage = store.Store(root)
            stack.callback(usage.engine.dispose)
            stack.callback(setattr, headless_agent, "_store", headless_agent._store)
            headless_agent._store = lambda: usage
            headless_agent.run_agent = partial(original, model=model)
        else:
            headless_agent.run_agent = codex_runner(codex_bin, model)  # type: ignore[assignment]
        yield model or (settings.agent_model() if name == "claude" else None) or "cli-default"
