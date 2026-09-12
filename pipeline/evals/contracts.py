"""Structural contracts for prompts assembled from independently owned layers."""

from __future__ import annotations

from dataclasses import dataclass
import re

_HEADING = re.compile(r"^(#{1,2})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")


@dataclass(frozen=True)
class _Heading:
    level: int
    title: str
    start: int
    end: int


def _headings(prompt: str) -> list[_Heading]:
    return [
        _Heading(len(match.group(1)), match.group(2), match.start(), match.end())
        for match in _HEADING.finditer(prompt)
    ]


def _top_level_sections(prompt: str, headings: list[_Heading]) -> dict[str, str]:
    top = [heading for heading in headings if heading.level == 1]
    return {
        heading.title: prompt[heading.end:top[index + 1].start if index + 1 < len(top)
                              else len(prompt)]
        for index, heading in enumerate(top)
    }


@dataclass(frozen=True)
class PromptContract:
    name: str
    subsections: tuple[tuple[str, tuple[str, ...]], ...] = ()
    placeholder_counts: tuple[tuple[str, int], ...] = ()
    output_fields: frozenset[str] = frozenset()

    def violations(self, prompt: str) -> list[str]:
        errors: list[str] = []
        headings = _headings(prompt)
        top = [heading.title for heading in headings if heading.level == 1]
        expected_top = ["Background", "Behavior", "Output"]
        if top != expected_top:
            errors.append(f"top-level sections are {top!r}, expected {expected_top!r}")

        sections = _top_level_sections(prompt, headings)
        for title in expected_top:
            if not sections.get(title, "").strip():
                errors.append(f"{title} section is empty")

        expected_subsections = dict(self.subsections)
        actual_subsections: dict[str, list[str]] = {title: [] for title in expected_top}
        current: str | None = None
        for heading in headings:
            if heading.level == 1:
                current = heading.title
            elif current in actual_subsections:
                actual_subsections[current].append(heading.title)
        for title, expected in expected_subsections.items():
            actual = actual_subsections.get(title, [])
            if actual != list(expected):
                errors.append(
                    f"{title} subsections are {actual!r}, expected {list(expected)!r}"
                )

        expected_placeholders = dict(self.placeholder_counts)
        actual_placeholders = frozenset(_PLACEHOLDER.findall(prompt))
        if actual_placeholders != frozenset(expected_placeholders):
            errors.append(
                f"placeholders are {sorted(actual_placeholders)!r}, "
                f"expected {sorted(expected_placeholders)!r}"
            )
        for placeholder, expected_count in expected_placeholders.items():
            count = prompt.count(placeholder)
            if count != expected_count:
                errors.append(
                    f"placeholder {placeholder} occurs {count} times, "
                    f"expected {expected_count}"
                )

        output = sections.get("Output", "")
        for field in sorted(self.output_fields):
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(field)}(?![A-Za-z0-9_])",
                         output) is None:
                errors.append(f"Output does not name field {field}")
        return errors


def assert_prompt_contract(contract: PromptContract, prompt: str) -> None:
    errors = contract.violations(prompt)
    if errors:
        details = "\n".join(f"- {error}" for error in errors)
        raise AssertionError(f"{contract.name} prompt contract failed:\n{details}")
