"""Parses Playwright's JSON report, which comes from the sandbox and is attacker-controlled."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, get_args

SpecStatus = Literal["passed", "failed", "flaky", "skipped", "timedOut", "interrupted"]

MAX_RESULTS = 5000
MAX_TEXT = 300
_MAX_ATTACHMENTS = 20
_OUT_PREFIX = "/tmp/e2e-out/"
_SEP = " › "
_STATUSES = frozenset(get_args(SpecStatus)) - {"flaky"}
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass(frozen=True)
class SpecResult:
    file: str
    title: str
    status: SpecStatus
    error: str | None
    attachments: list[str]


@dataclass(frozen=True)
class E2eRun:
    booted: bool
    results: list[SpecResult]
    problem: str | None


def _no_run(problem: str) -> E2eRun:
    return E2eRun(booted=False, results=[], problem=problem)


def _dicts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]


def _text(value: object) -> str:
    return value[:MAX_TEXT] if isinstance(value, str) else ""


def _safe_file(file: object) -> str | None:
    if not isinstance(file, str) or not file or file.startswith(("/", "\\")):
        return None
    if re.match(r"^[A-Za-z]:", file) or any(seg == ".." for seg in re.split(r"[\\/]", file)):
        return None
    return file


def _status(test: dict[str, object], results: list[dict[str, object]]) -> SpecStatus:
    outcome = test.get("status")
    if outcome == "flaky":
        return "flaky"
    if outcome == "skipped":
        return "skipped"
    last = results[-1].get("status") if results else None
    if outcome == "expected" and test.get("expectedStatus") != "passed" and last in ("passed", "failed"):
        return "passed"
    if len(results) > 1 and last == "passed" and any(r.get("status") != "passed" for r in results[:-1]):
        return "flaky"
    if isinstance(last, str) and last in _STATUSES:
        return last  # pyright: ignore[reportReturnType]
    return "failed"


def _error(results: list[dict[str, object]]) -> str | None:
    for result in reversed(results):
        errors = _dicts(result.get("errors")) or _dicts([result.get("error")])
        for err in errors:
            message = err.get("message")
            if isinstance(message, str) and message.strip():
                return _text(_ANSI.sub("", message.strip()).split("\n", 1)[0]) or None
    return None


def _attachments(results: list[dict[str, object]]) -> list[str]:
    names: list[str] = []
    for result in results:
        for att in _dicts(result.get("attachments")):
            path = att.get("path")
            if att.get("contentType") != "image/png" or not isinstance(path, str):
                continue
            if not path.startswith(_OUT_PREFIX):
                continue
            rel = path[len(_OUT_PREFIX) :]
            if any(seg in ("", ".", "..") for seg in rel.split("/")) or rel in names:
                continue
            names.append(rel)
    return names[:_MAX_ATTACHMENTS]


def _titles(path: tuple[str, ...], title: object) -> str:
    return _text(_SEP.join((*path, title if isinstance(title, str) else "")))


def parse_report(report: bytes | None, *, output_tail: str) -> E2eRun:
    if report is None:
        errors = [ln.strip() for ln in output_tail.splitlines() if ln.strip().startswith("Error:")]
        return _no_run(errors[-1][:MAX_TEXT] if errors else "no report")
    try:
        data: object = json.loads(report)
    except (ValueError, RecursionError):
        return _no_run("report is not valid JSON")
    if not isinstance(data, dict) or not isinstance(data.get("suites"), list):
        return _no_run("report is not an object with a suites list")

    results: list[SpecResult] = []
    dropped = 0
    capped = False
    # (suite, title path of its ancestors, nearest file)
    stack: list[tuple[dict[str, object], tuple[str, ...], str | None]] = [
        (s, (), None) for s in reversed(_dicts(data["suites"]))
    ]
    while stack and not capped:
        suite, path, file = stack.pop()
        title = suite.get("title")
        raw_file = suite.get("file")
        suite_file = raw_file if isinstance(raw_file, str) else file
        here = path if not isinstance(title, str) or title == raw_file else (*path, title)
        for spec in _dicts(suite.get("specs")):
            spec_file = _safe_file(spec.get("file") if isinstance(spec.get("file"), str) else suite_file)
            for test in _dicts(spec.get("tests")):
                if spec_file is None:
                    dropped += 1
                    continue
                if len(results) >= MAX_RESULTS:
                    capped = True
                    break
                rs = _dicts(test.get("results"))
                results.append(
                    SpecResult(
                        file=spec_file[:MAX_TEXT],
                        title=_titles(here, spec.get("title")),
                        status=_status(test, rs),
                        error=_error(rs),
                        attachments=_attachments(rs),
                    )
                )
            if capped:
                break
        stack.extend((c, here, suite_file) for c in reversed(_dicts(suite.get("suites"))))

    notes: list[str] = []
    if capped:
        notes.append(f"stopped at {MAX_RESULTS} results")
    if dropped:
        notes.append(f"dropped {dropped} results with an unsafe file path")
    return E2eRun(booted=True, results=results, problem="; ".join(notes) or None)
