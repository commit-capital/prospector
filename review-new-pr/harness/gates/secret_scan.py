"""secret_scan gate — detect credentials newly added by a PR.

Strategy: scan the *added* lines of every changed file's patch for
high-confidence credential patterns. Avoids the trap of flagging
pre-existing secrets that didn't change.

In production (GitHub Actions), gitleaks runs alongside this with broader
coverage. This gate is the harness-layer detector with patterns specifically
tuned for common provider keys (Anthropic, OpenAI, GitHub, AWS, generic
high-entropy).
"""
from __future__ import annotations
import re
from collections.abc import Iterator
from ._common import PRContext, gate_result


# Each pattern: (name, severity, compiled regex)
# Patterns aim for low false-positive rate — match the structural shape of a
# real credential, not just a keyword.
SECRET_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    (
        "anthropic_api_key",
        "critical",
        re.compile(r"\bsk-ant-[A-Za-z0-9_-]{50,}\b"),
    ),
    (
        "openai_api_key",
        "critical",
        re.compile(r"\bsk-[A-Za-z0-9]{20,}T3BlbkFJ[A-Za-z0-9]{20,}\b"),
    ),
    (
        "github_token_classic",
        "critical",
        re.compile(r"\bghp_[A-Za-z0-9]{36,}\b"),
    ),
    (
        "github_token_fine_grained",
        "critical",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{80,}\b"),
    ),
    (
        "github_oauth_token",
        "critical",
        re.compile(r"\bgho_[A-Za-z0-9]{36,}\b"),
    ),
    (
        "aws_access_key",
        "critical",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "aws_secret_access_key",
        "critical",
        # AWS secret keys are 40 base64 chars but unanchored matches are noisy;
        # require the key=value context.
        re.compile(
            r"aws_secret_access_key\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?",
            re.IGNORECASE,
        ),
    ),
    (
        "google_api_key",
        "high",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ),
    # A private key (private_key_block, critical), whose body may run onto the
    # lines after its header, is read outside this list in _private_keys.
    (
        "jwt_token",
        "medium",
        # JWTs are heuristically detectable as 3 base64url segments.
        # We classify medium because lots of test fixtures embed example JWTs;
        # the LLM gate downstream can distinguish.
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    (
        "slack_token",
        "critical",
        re.compile(r"\bxox[abpros]-[A-Za-z0-9-]{10,}\b"),
    ),
]


# Common false-positive patterns. If the matched secret falls inside a comment
# referencing one of these, downgrade to low/uncertain.
TEST_FIXTURE_HINTS = [
    "example",
    "sample",
    "fixture",
    "test_",
    "redacted",
    "***",
    "your-key-here",
    "<your-",
    "abcdef0123",
]

# File paths that are intrinsically test/fixture/docs — credentials there are
# almost always fake by construction. We downgrade aggressively.
FIXTURE_PATH_HINTS = [
    "__tests__",
    "/tests/",
    "/test/",
    "/spec/",
    "/__mocks__/",
    "/fixtures/",
    "/examples/",
    ".test.",
    ".spec.",
    "/docs/",
    "/doc/",
    "/README",
    "/CHANGELOG",
]


def _is_likely_fixture(line: str) -> bool:
    lc = line.lower()
    return any(hint in lc for hint in TEST_FIXTURE_HINTS)


def _is_fixture_path(filename: str) -> bool:
    """A credential in a fixture-shaped file path is overwhelmingly a fake.

    True positive rate of real credentials committed to /tests/ or /docs/ is
    near zero — and the cost of flagging real i18n contributors who put
    fake JWTs in their tests is high. We trust the path.
    """
    if not filename:
        return False
    fn = filename.lower()
    return any(hint in fn for hint in FIXTURE_PATH_HINTS)


# A private key is its header and the base64 body after it; the header alone is
# a validation message, a placeholder, or a parser's constant. _KEY_LEAD is what
# may stand between the two when a PEM block is spread over a string literal or
# a JSON value — whitespace, quotes, `+`, commas, backslashes, \r and \n escapes
# — and the armor headers of an encrypted PEM or a PGP block, each read up to a
# backslash or the next `-----`. Its quantifiers are possessive and it stops
# short of the next header, so a line's headers are each read past once.
# _KEY_BODY is 40 characters of the base64 alphabet: a PEM body line runs 64
# columns and an OpenSSH one 70, while an elided or stub body is shorter. This
# is pipeline/threats.py's private-key check, restated because the harness
# imports nothing from pipeline.
_KEY_HEADER = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")
_KEY_GAP = r"""(?:\\[rn]|[\s"'`+,\\])*+"""
_KEY_LEAD = re.compile(
    _KEY_GAP
    + r"(?:(?:Proc-Type|DEK-Info|Version|Comment|Hash|Charset|MessageID):"
      r"[^\\-]*+(?:-(?!----)[^\\-]*+)*+" + _KEY_GAP + r")*+")
_KEY_BODY = re.compile(r"[A-Za-z0-9+/]{40}")
# How many added lines after a header-ending line _private_keys reads for the
# body: an encrypted PEM's or a PGP block's armor headers and blank line come
# first.
_KEY_LOOKAHEAD = 6


def _key_body_opens(line: str, pos: int) -> bool | None:
    """Whether `line` from `pos`, past what may stand before a key's body,
    opens on that body: None when nothing else follows to the line's end."""
    lead = _KEY_LEAD.match(line, pos)
    end = lead.end() if lead else pos
    if _KEY_BODY.match(line, end):
        return True
    return None if end == len(line) else False


def _private_keys(added: str, following: list[str]) -> Iterator[re.Match[str]]:
    """The private-key headers in this added line that have their base64 body
    after them: in the line itself, or opening one of `following`, the added
    lines directly after it, past blank and armor-header lines."""
    for header in _KEY_HEADER.finditer(added):
        opens = _key_body_opens(added, header.end())
        for line in following:
            if opens is not None:
                break
            opens = _key_body_opens(line, 0)
        if opens:
            yield header


def _added_runs(patch: str) -> Iterator[list[str]]:
    """Each run of consecutive added lines (starting with '+', not '+++') in
    `patch`, without their '+'."""
    run: list[str] = []
    for raw_line in patch.split("\n"):
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            run.append(raw_line[1:])
        elif run:
            yield run
            run = []
    if run:
        yield run


def _scan_added_lines(patch: str, filename: str = "") -> list[dict]:
    """Findings against the added lines of `patch`."""
    findings: list[dict] = []
    fixture_path = _is_fixture_path(filename)
    for run in _added_runs(patch):
        for i, added in enumerate(run):
            matches = [(name, severity, m)
                       for name, severity, pat in SECRET_PATTERNS for m in pat.finditer(added)]
            matches += [("private_key_block", "critical", m)
                        for m in _private_keys(added, run[i + 1:i + 1 + _KEY_LOOKAHEAD])]
            if not matches:
                continue
            # Fixture-like: either the line itself hints at fixture, OR
            # the file path is intrinsically test/docs/examples.
            is_fixture_like = _is_likely_fixture(added) or fixture_path
            snippet = (added[:120] + "…") if len(added) > 120 else added
            for name, severity, m in matches:
                findings.append({
                    "pattern": name,
                    "severity": severity,
                    "snippet": snippet,
                    "match": m.group(0)[:20] + "…",  # never log the full secret
                    "fixture_like": is_fixture_like,
                    "fixture_path": fixture_path,
                })
    return findings


def run(ctx: PRContext) -> dict:
    """Run secret_scan against a PR context. Returns a gate_result dict."""
    all_findings: list[dict] = []
    for f in ctx.files or []:
        patch = f.get("patch") or ""
        if not patch:
            continue
        filename = f.get("filename") or ""
        for finding in _scan_added_lines(patch, filename):
            finding["filename"] = filename or "?"
            all_findings.append(finding)

    if not all_findings:
        return gate_result("pass", evidence="No credential patterns in added lines.")

    # Classify: any real (non-fixture) critical finding -> fail/critical
    real_critical = [
        f for f in all_findings
        if f["severity"] == "critical" and not f["fixture_like"]
    ]
    if real_critical:
        first = real_critical[0]
        return gate_result(
            "fail",
            severity="critical",
            evidence=(
                f"Likely real credential added in `{first['filename']}` "
                f"(pattern: {first['pattern']}). "
                f"{len(real_critical)} critical finding(s) total."
            ),
            details={"findings": all_findings},
        )

    # All findings are fixture-like or medium severity — flag for review but
    # don't auto-reject.
    return gate_result(
        "uncertain",
        severity="medium",
        evidence=(
            f"{len(all_findings)} credential-shaped string(s) added but all "
            "appear to be test fixtures / example values. Human should confirm."
        ),
        details={"findings": all_findings},
    )
