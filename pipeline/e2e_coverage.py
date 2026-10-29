"""Classifies repeated e2e suite runs per test and renders the coverage report.

The report is generated output: never stored, never parsed back."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pipeline import wire
from pipeline.e2e_report import E2eRun, SpecResult

Verdict = Literal["reliable", "flaky", "failing"]
Cause = Literal["network", "secret", "sandbox", "timeout", "unknown"]

_VERDICTS: tuple[Verdict, ...] = ("reliable", "flaky", "failing")
_EVIDENCE_CHARS = 200

_CAUSES: list[tuple[Cause, re.Pattern[str]]] = [
    ("sandbox", re.compile(
        r"Failed to launch|cannot open shared object|browserType\.launch"
        r"|Executable doesn't exist", re.I)),
    ("network", re.compile(
        r"ENOTFOUND|ECONNREFUSED|ECONNRESET|EAI_AGAIN|getaddrinfo|net::ERR_", re.I)),
    ("secret", re.compile(
        r"\b40[13]\b|unauthorized|forbidden|api[ _-]?key|secret|credential", re.I)),
    ("timeout", re.compile(r"timeout of \d+ms|timed out|exceeded", re.I)),
]


@dataclass(frozen=True)
class SpecCoverage:
    file: str
    title: str
    verdict: Verdict
    passes: int
    runs: int
    cause: Cause | None
    evidence: str | None
    screenshot: str | None


def cause_hint(error: str) -> Cause:
    """A guess for the operator to check, never a decision."""
    for cause, pattern in _CAUSES:
        if pattern.search(error):
            return cause
    return "unknown"


def _counted(runs: list[E2eRun]) -> list[E2eRun]:
    return [r for r in runs if r.booted and r.results]


def boot_failures(runs: list[E2eRun]) -> list[str]:
    return [r.problem or "no results" for r in runs if not (r.booted and r.results)]


def skipped_count(runs: list[E2eRun]) -> int:
    return len({(r.file, r.title) for run in _counted(runs) for r in run.results
                if r.status == "skipped"} - _non_skipped(runs))


def _non_skipped(runs: list[E2eRun]) -> set[tuple[str, str]]:
    return {(r.file, r.title) for run in _counted(runs) for r in run.results
            if r.status != "skipped"}


def _first_line(error: str) -> str:
    line = next((ln.strip() for ln in error.splitlines() if ln.strip()), "")
    return line[:_EVIDENCE_CHARS]


def _verdict(passes: int, runs: int, any_flaky: bool) -> Verdict:
    if any_flaky:
        return "flaky"
    if passes == runs:
        return "reliable"
    return "failing" if passes == 0 else "flaky"


def classify(runs: list[E2eRun]) -> list[SpecCoverage]:
    counted = _counted(runs)
    keys = sorted(_non_skipped(counted))
    out: list[SpecCoverage] = []
    for key in keys:
        seen: list[SpecResult | None] = [
            next((r for r in run.results if (r.file, r.title) == key), None)
            for run in counted]
        passes = sum(1 for r in seen if r is not None and r.status == "passed")
        any_flaky = any(r is not None and r.status == "flaky" for r in seen)
        verdict = _verdict(passes, len(counted), any_flaky)
        cause: Cause | None = None
        evidence: str | None = None
        screenshot: str | None = None
        if verdict != "reliable":
            bad = next((r for r in seen if r is not None and r.status != "passed"), None)
            cause = cause_hint(bad.error) if bad and bad.error else "unknown"
            if bad and bad.status == "timedOut" and cause == "unknown":
                cause = "timeout"
            evidence = _first_line(bad.error) if bad and bad.error else None
            screenshot = next((a for r in seen if r is not None
                               and r.status != "passed" for a in r.attachments), None)
        out.append(SpecCoverage(key[0], key[1], verdict, passes, len(counted),
                                cause, evidence, screenshot))
    return out


def baseline(cov: list[SpecCoverage]) -> tuple[dict[str, list[str]], list[wire.VerifyE2eUncovered]]:
    """The reliable tests as {spec file: titles}, and every other test with
    its verdict and cause."""
    reliable: dict[str, list[str]] = {}
    uncovered: list[wire.VerifyE2eUncovered] = []
    for c in cov:
        if c.verdict == "reliable":
            reliable.setdefault(c.file, []).append(c.title)
        else:
            uncovered.append({"file": c.file, "title": c.title, "verdict": c.verdict,
                              "cause": c.cause, "evidence": c.evidence})
    return reliable, uncovered


def _cell(text: str | None) -> str:
    return " ".join((text or "").split()).replace("|", "\\|")


def _headline(cov: list[SpecCoverage], boot_failures: list[str], runs: int) -> str:
    if len(boot_failures) >= runs:
        return f"no run booted ({len(boot_failures)} of {runs} runs)"
    if not cov:
        return "no specs ran"
    reliable = sum(1 for c in cov if c.verdict == "reliable")
    return f"{reliable} of {len(cov)} specs reliable"


def render(cov: list[SpecCoverage], *, base_sha: str, boot_failures: list[str], runs: int,
           skipped: int = 0, notes: list[str] | None = None) -> str:
    lines = [f"# E2E coverage at {base_sha}: {_headline(cov, boot_failures, runs)}", ""]
    for verdict in _VERDICTS:
        rows = [c for c in cov if c.verdict == verdict]
        if not rows:
            continue
        lines += [f"## {verdict.capitalize()} ({len(rows)})", "",
                  "| Spec | Passes | Cause | Evidence | Screenshot |",
                  "|---|---|---|---|---|"]
        lines += [f"| {_cell(f'{c.file} › {c.title}')} | {c.passes}/{c.runs} "
                  f"| {c.cause or ''} | {_cell(c.evidence)} | {_cell(c.screenshot)} |"
                  for c in rows]
        lines.append("")
    lines.append(f"{skipped} skipped")
    if boot_failures:
        lines += ["", "## Runs that did not count", ""]
        lines += [f"- {_cell(p)}" for p in boot_failures]
    if notes:
        lines += ["", "## Notes", ""]
        lines += [f"- {_cell(n)}" for n in notes]
    return "\n".join(lines) + "\n"
