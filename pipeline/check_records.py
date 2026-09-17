"""Bounded JSONL records of the sandbox runs an authoring agent made, keyed by
the file they are kept in. The agent's check tool appends one record per run;
the worker that launched the agent collects them once it returns."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict


class CheckRecord(TypedDict):
    """One sandbox run an authoring agent made. `exit` is the sandbox's sentinel
    exit when the command ran and None when it did not; `error_kind` names why
    it did not — `refused` for a run the check tool declined to make, the kind
    the run itself names when it names one, else `infrastructure`."""
    kind: str
    files: list[str]
    cmd: str | None
    exit: int | None
    error_kind: str | None
    error: str | None
    error_excerpt: str | None
    duration_s: float | None
    at: str


def append(path: Path, record: CheckRecord, *, tool: str = "sandbox-check") -> None:
    """Append `record` to `path`. Best-effort: a record that cannot be written
    is reported on stderr under `tool`, the name the agent knows its check
    command by, and the run's verdict still reaches the agent."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        print(f"{tool}: the run could not be recorded: {e}", file=sys.stderr)


def collect(path: Path, limit: int) -> list[dict]:
    """The records in `path`, oldest first and at most `limit`, consuming the
    file so the next request starts empty. A line that is not a JSON object is
    skipped."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    path.unlink(missing_ok=True)
    out: list[dict] = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out[:limit]
