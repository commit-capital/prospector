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
    (
        "private_key_block",
        "critical",
        re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    ),
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


def _scan_added_lines(patch: str, filename: str = "") -> list[dict]:
    """Yield findings against added lines (lines starting with '+', not '+++')."""
    findings: list[dict] = []
    fixture_path = _is_fixture_path(filename)
    for raw_line in patch.split("\n"):
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        added = raw_line[1:]  # strip leading '+'
        for name, severity, pat in SECRET_PATTERNS:
            for m in pat.finditer(added):
                # Fixture-like: either the line itself hints at fixture, OR
                # the file path is intrinsically test/docs/examples.
                is_fixture_like = (
                    _is_likely_fixture(added) or fixture_path
                )
                findings.append({
                    "pattern": name,
                    "severity": severity,
                    "snippet": (added[:120] + "…") if len(added) > 120 else added,
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
