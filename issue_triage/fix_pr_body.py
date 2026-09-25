"""The title, body and commit message of a pull request the issue-fix lane
proposes, written by the host from the lane's recorded evidence.

Every section heading, every fact and every checklist tick is the host's: the
repository's required sections (`describe_pr.required_sections`), the issue it
fixes, the tests that reproduce it and the runs that proved the fix, the models
that wrote it. The agents contribute only three short fields — the summary, the
root cause, and each changed file's rationale — and those are text an outsider's
report shaped, so `_clean` holds them to one line of plain text before they
land. `problems` is the gate every rendering passes before it is proposed.
"""
from __future__ import annotations

import re

from issue_triage import link_prs
from pipeline import describe_pr, settings

TITLE_MAX = 120
FIELD_MAX = 400
BODY_MAX = 20_000
MARKER = "<!-- prospector:issue-fix v1 issue={issue} base={base} report={report} -->"

# Characters that reorder or hide text in a rendered page.
_BIDI_RE = re.compile("[\u202a-\u202e\u2066-\u2069\u200e\u200f]")
_LINK_RE = re.compile(r"https?://[^\s)>\]]+")
_HTML_RE = re.compile(r"<(?!!--)[a-zA-Z/][^>]*>")
_IMAGE_RE = re.compile(r"!\[")
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MENTION_RE = re.compile(r"(?<![\w`])@(?=[A-Za-z0-9-])")


def _clean(text: str, limit: int = FIELD_MAX) -> str:
    """Agent-written text as one line of plain Markdown-inert prose: no line
    breaks, HTML, images, links, bidi controls or live @mentions."""
    one = _BIDI_RE.sub("", str(text))
    one = _HTML_RE.sub("", one)
    one = _MD_LINK_RE.sub(r"\1", one)
    one = _IMAGE_RE.sub("[", one)
    one = _LINK_RE.sub("(link removed)", one)
    one = _MENTION_RE.sub("@\u200b", one)
    one = " ".join(one.replace("`", "'").split())
    return one[:limit].rstrip()


def title(issue: int, summary: str) -> str:
    return _clean(f"fix: {summary}", TITLE_MAX) or f"fix: issue #{issue}"


def commit_message(issue: int, summary: str) -> str:
    return f"{title(issue, summary)}\n\nFixes #{issue}\n"


def _verification(result: dict, tests: list[str], test_cmd: str | None) -> list[str]:
    proof = result.get("proof") or {}
    lines = []
    if tests:
        lines.append("- Reproducing test(s), added in this PR: " + ", ".join(
            f"`{t}`" for t in tests))
        lines.append("- They fail on the base commit and pass with the fix, each twice in a "
                     "fresh sandbox" + (f" (`{test_cmd}`)" if test_cmd else "") + ".")
    agreement = result.get("agreement")
    if agreement:
        lines.append(f"- {len(result.get('candidates') or [])} independent attempts wrote "
                     f"{len(agreement.get('reproductions') or [])} reproductions; this fix "
                     "passes every one of them.")
    compiled = proof.get("compile")
    if compiled:
        lines.append("- The repository's compile command passes with the fix applied."
                     if compiled.get("exit") == 0
                     else "- The compile command fails on the base commit too; this fix "
                          "adds no new failure there.")
    related = proof.get("related_tests")
    if related:
        lines.append(f"- {len(related.get('files') or [])} existing test file(s) near the "
                     "change pass.")
    suite = proof.get("suite")
    if suite and not suite.get("skipped"):
        lines.append(f"- The full test suite passes except for the {suite.get('excluded', 0)} "
                     "file(s) that already fail on the base commit.")
    return lines or ["- (no verification was recorded)"]


# Which content block a template section carries, by a word in its heading;
# the first match wins.
_SECTION_ROLES = (("issue", "issue"), ("linked", "issue"), ("change", "changes"),
                  ("verif", "verification"), ("test", "verification"), ("risk", "risks"),
                  ("model", "model"), ("summary", "summary"), ("thinking", "summary"),
                  ("why", "summary"), ("description", "summary"))
_DEFAULT_HEADINGS = {"summary": "Summary", "issue": "Linked Issues", "changes": "What Changed",
                     "verification": "Verification", "risks": "Risks", "model": "Model Used"}


def _role(heading: str) -> str | None:
    low = heading.lower()
    return next((role for word, role in _SECTION_ROLES if word in low), None)


def render(*, issue: int, result: dict, tests: list[str], base_sha: str, report_sha: str,
           models: list[str], test_cmd: str | None) -> str:
    """The pull request body: a disclosure, the repository's required sections
    (`describe_pr.required_sections`) each filled from `result` by its heading,
    any content block no required section carries under its own heading, and
    the marker that names what it came from."""
    changes = [f"- `{_clean(c.get('path', ''), 200)}`: {_clean(c.get('rationale', ''))}"
               for c in (result.get("changes") or [])]
    model_list = ", ".join(sorted(set(models))) or "unrecorded"
    blocks = {
        "summary": [
            f"- Issue #{issue} reports a defect in {settings.repo()}.",
            f"- Root cause, as the fixing agent read it: {_clean(result.get('root_cause', ''))}",
            f"- This pull request: {_clean(result.get('summary', ''))}",
        ],
        "issue": [f"Fixes #{issue}"],
        "changes": changes or ["- (no change recorded)"],
        "verification": _verification(result, tests, test_cmd),
        "risks": [
            "- Written by an automated pipeline, not a person: review it as you would an "
            "outside contribution.",
            f"- Risk tier of the touched paths: {(result.get('tier') or {}).get('tier', '?')}.",
        ],
        "model": [f"- {model_list} (Anthropic Claude, via Claude Code), writing and "
                  "checking the change in an isolated sandbox."],
    }
    sections: list[tuple[str, list[str]]] = []
    carried: set[str] = set()
    for heading in describe_pr.required_sections():
        role = _role(heading)
        if role is None or role in carried:
            sections.append((heading, ["- n/a"]))
        else:
            sections.append((heading, blocks[role]))
            carried.add(role)
    sections += [(_DEFAULT_HEADINGS[role], lines) for role, lines in blocks.items()
                 if role not in carried]
    parts = [
        "> [!NOTE]",
        "> Opened by Prospector's automated issue-fix pipeline. A maintainer reviews "
        "and decides; nothing here merges on its own.",
        "",
    ]
    for heading, lines in sections:
        parts += [f"## {heading}", "", *lines, ""]
    parts.append(MARKER.format(issue=issue, base=base_sha[:12], report=report_sha))
    return "\n".join(parts) + "\n"


def problems(title_text: str, body: str, message: str, *, issue: int) -> list[str]:
    """Why a rendering may not be proposed; empty when it may."""
    out: list[str] = []
    if not title_text or "\n" in title_text or len(title_text) > TITLE_MAX:
        out.append("the title is empty, multi-line, or too long")
    if len(body) > BODY_MAX:
        out.append("the body is too long")
    missing = describe_pr.missing_sections(body, describe_pr.required_sections())
    if missing:
        out.append(f"the body lacks required sections: {', '.join(missing)}")
    for name, text in (("body", body), ("commit message", message)):
        refs = link_prs.parse_issue_refs(text)
        if refs != {issue}:
            out.append(f"the {name} closes {sorted(refs)} rather than exactly #{issue}")
    if _BIDI_RE.search(body):
        out.append("the body carries bidi control characters")
    if _HTML_RE.search(body) or _IMAGE_RE.search(body):
        out.append("the body carries HTML or an image")
    repo_url = f"https://github.com/{settings.repo()}"
    foreign = [u for u in _LINK_RE.findall(body) if not u.startswith(repo_url)]
    if foreign:
        out.append(f"the body links outside {settings.repo()}")
    if re.search("(?<![\\w`\u200b])@[A-Za-z0-9-]", body):
        out.append("the body carries a live @mention")
    return out
