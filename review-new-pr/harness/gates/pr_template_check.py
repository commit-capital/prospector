"""pr_template_check gate — verify the PR body follows the repository's PR template.

The required-section vocabulary comes from the repository profile
(`_common.template_sections`, selected by TRIAGE_PROFILE). With no policy
configured the gate passes trivially. Otherwise it parses the PR body and
checks each required section is present AND substantive (not just a
copied-template placeholder).

Verdict mapping:
- pass: all required sections present with substantive content
- uncertain: 1–2 sections missing or boilerplate-only
- fail: 3+ sections missing/boilerplate, OR the entire body is the unedited template
"""
from __future__ import annotations
import re
from ._common import PRContext, gate_result, template_sections, trusted_authors


# A section is detected by a markdown header containing the section name.
# Be permissive about formatting: `## Section Name`, `### section name`, etc.
def _section_pattern(name: str) -> re.Pattern:
    escaped = re.escape(name)
    return re.compile(rf"^\s*#{{1,6}}\s*\**\s*{escaped}\s*\**\s*$", re.IGNORECASE | re.MULTILINE)


# Heuristics for "section is just the template boilerplate":
# - Empty body under header
# - Single placeholder line like "TBD", "N/A", "(describe...)"
# - Verbatim copy of the template hint text
BOILERPLATE_INDICATORS = [
    r"^\s*tbd\s*$",
    r"^\s*n/?a\s*$",
    r"^\s*to ?do\s*$",
    r"^\s*\(.+\)\s*$",                       # parenthesized hint, e.g. "(describe ...)"
    r"^\s*placeholder\s*$",
    r"^\s*\.\.\.\s*$",
    r"^\s*<.+>\s*$",                          # angle-bracket placeholder
    r"^\s*list of changes here\s*$",
    r"^\s*describe.+here\s*$",
]
BOILERPLATE_RE = re.compile("|".join(BOILERPLATE_INDICATORS), re.IGNORECASE)


def _extract_section_content(body: str, section: str) -> str:
    """Return the content under a section header until the next header.

    Returns "" if the section header is not present in body.
    """
    pat = _section_pattern(section)
    m = pat.search(body)
    if not m:
        return ""

    start = m.end()
    next_header_re = re.compile(r"^\s*#{1,6}\s+", re.MULTILINE)
    next_m = next_header_re.search(body, start)
    end = next_m.start() if next_m else len(body)
    return body[start:end].strip()


def _is_substantive(content: str) -> bool:
    """Heuristic: substantive iff at least one non-boilerplate line exists.

    Sections may use blockquote bullets (`> - foo`), so strip `>` markers
    before evaluating. We err toward permissive — only obvious template
    placeholders (TBD, N/A, "(describe here)", etc.) are non-substantive.
    Short genuine answers like "Low. Tests cover this." are accepted.
    """
    if not content:
        return False

    # Normalize: strip leading `>` blockquote markers, drop empty lines
    lines: list[str] = []
    for raw in content.split("\n"):
        stripped = raw.strip()
        if not stripped:
            continue
        normalized = stripped.lstrip(">").strip()
        if normalized:
            lines.append(normalized)

    if not lines:
        return False

    # Substantive if at least one line is not a boilerplate placeholder
    return any(not BOILERPLATE_RE.match(line) for line in lines)


def _looks_like_unedited_template(body: str, required: list[str]) -> bool:
    """If nearly all required section headers exist AND none has substantive
    content, the contributor clearly pasted the template and didn't fill it in."""
    headers_present = sum(
        1 for s in required if _section_pattern(s).search(body)
    )
    substantive_count = sum(
        1
        for s in required
        if _is_substantive(_extract_section_content(body, s))
    )
    return headers_present >= max(2, len(required) - 1) and substantive_count == 0


def run(ctx: PRContext) -> dict:
    required, _recommended = template_sections()

    if not required:
        return gate_result(
            "pass",
            evidence="No PR-template policy configured; gate not applicable.",
        )

    # the template is contributor guidance owned by the maintainers — a trusted
    # maintainer's own PR is not its subject
    if ctx.author and ctx.author in trusted_authors():
        return gate_result(
            "pass",
            evidence=f"Author {ctx.author} is a trusted maintainer; the PR-template "
                     "policy does not apply.",
        )

    body = ctx.body or ""

    if not body.strip():
        return gate_result(
            "fail",
            severity="high",
            evidence="PR body is empty; every required template section is missing.",
            details={"missing": required},
        )

    if _looks_like_unedited_template(body, required):
        return gate_result(
            "fail",
            severity="high",
            evidence=(
                "PR body appears to be the unedited template — section headers "
                "are present but none have substantive content."
            ),
            details={"required_sections": required},
        )

    missing: list[str] = []
    boilerplate: list[str] = []
    for section in required:
        content = _extract_section_content(body, section)
        if not content:
            missing.append(section)
        elif not _is_substantive(content):
            boilerplate.append(section)

    issues_count = len(missing) + len(boilerplate)

    if issues_count == 0:
        return gate_result(
            "pass",
            evidence="All required PR template sections present and substantive.",
        )

    if issues_count >= 3:
        return gate_result(
            "fail",
            severity="medium",
            evidence=(
                f"{issues_count} required section(s) missing or boilerplate-only: "
                f"missing={missing}, boilerplate={boilerplate}. "
                "Ask contributor to fill out the template before review."
            ),
            details={"missing": missing, "boilerplate": boilerplate},
        )

    # 1 or 2 issues — uncertain, surface to human as a low-friction comment.
    return gate_result(
        "uncertain",
        severity="low",
        evidence=(
            f"PR has {issues_count} template section(s) missing or boilerplate-only: "
            f"missing={missing}, boilerplate={boilerplate}. Otherwise OK."
        ),
        details={"missing": missing, "boilerplate": boilerplate},
    )
