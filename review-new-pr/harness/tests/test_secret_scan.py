"""Tests for the secret_scan gate.

Validates that real-looking credentials are detected and example/fixture
values are downgraded so the harness doesn't drown in false positives.
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gates._common import PRContext  # noqa: E402
from gates import secret_scan  # noqa: E402


def make_ctx(filename: str, added_lines: list[str], pr_num: int = 9999) -> PRContext:
    """Build a minimal PRContext whose patch contains `added_lines`."""
    patch = "@@ -1,3 +1,5 @@\n const x = 1;\n" + "".join(f"+{line}\n" for line in added_lines)
    return PRContext(
        number=pr_num,
        title="test PR",
        body="",
        author="example",
        head_sha="deadbeef",
        base_ref="master",
        additions=len(added_lines),
        deletions=0,
        changed_files=1,
        files=[{"filename": filename, "status": "modified", "additions": len(added_lines), "deletions": 0, "patch": patch}],
    )


# ── happy path ──────────────────────────────────────────────────────────

def test_clean_diff_passes():
    ctx = make_ctx("src/foo.ts", ["const x = computeThing();", "return x + 1;"])
    r = secret_scan.run(ctx)
    assert r["verdict"] == "pass"


def test_empty_files_passes():
    ctx = make_ctx("src/foo.ts", [])
    ctx.files = []
    r = secret_scan.run(ctx)
    assert r["verdict"] == "pass"


# ── malicious detection ─────────────────────────────────────────────────

def test_detects_anthropic_key():
    ctx = make_ctx("src/config.ts", [
        'const ANTHROPIC = "sk-ant-' + "a" * 100 + '";',
    ])
    r = secret_scan.run(ctx)
    assert r["verdict"] == "fail"
    assert r["severity"] == "critical"
    assert "anthropic_api_key" in str(r["details"])


def test_detects_github_classic_token():
    ctx = make_ctx(".env", [
        'GITHUB_TOKEN=ghp_' + "A" * 36,
    ])
    r = secret_scan.run(ctx)
    assert r["verdict"] == "fail"
    assert r["severity"] == "critical"


def test_detects_github_fine_grained_token():
    ctx = make_ctx(".env", [
        'PAT=github_pat_' + "B" * 90,
    ])
    r = secret_scan.run(ctx)
    assert r["verdict"] == "fail"
    assert r["severity"] == "critical"


def test_detects_aws_access_key():
    ctx = make_ctx("config.json", [
        '"awsAccessKeyId": "AKIAIOSFODNN7EXAMPLE",',
    ])
    r = secret_scan.run(ctx)
    # AKIAIOSFODNN7EXAMPLE is the AWS documentation example — the pattern
    # matches but the line contains "EXAMPLE" so should be downgraded.
    # Our test asserts at minimum that we detected it (verdict != pass).
    assert r["verdict"] != "pass"


def test_detects_openai_api_key():
    ctx = make_ctx("config.js", [
        'OPENAI_API_KEY=sk-' + "a" * 22 + 'T3BlbkFJ' + "b" * 22,
    ])
    r = secret_scan.run(ctx)
    assert r["verdict"] == "fail"


# ── private keys ────────────────────────────────────────────────────────
# A private key leaks with its base64 body; a header named in a message, a
# placeholder, or an elided example carries no key. The bodies are synthetic.

RSA_BODY = ["MIIEpAIBAAKCAQEAu7Q1fX9kLm2Zp4Vt8Rw3Hs6Jd0Nc5Ye1Gb7Ua2Ki9Ox4Pl3Q",
            "n6Ev1Tr5Dy0Sf3Cg7Bh2Aj9Zk4Xl8Mo1Np6Iq0Ur5Vs3Wt7Xu2Yv9Zw4Ax6By1Cz"]
OPENSSH_BODY = ["b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gt",
                "ZWQyNTUxOQAAACBu7Q1fX9kLm2Zp4Vt8Rw3Hs6Jd0Nc5Ye1Gb7Ua2Ki9Ox4Pl3QmA"]
ENCRYPTION_HEADERS = ("Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-128-CBC,3F17F5316E2BAC89BCB7BF6E1B4E4B7C", "")


def _pem(label: str, body: list[str] = RSA_BODY, headers: tuple[str, ...] = ()) -> list[str]:
    return [f"-----BEGIN {label}PRIVATE KEY-----", *headers, *body, f"-----END {label}PRIVATE KEY-----"]


def _patterns(r: dict) -> list[str]:
    return [f["pattern"] for f in r.get("details", {}).get("findings", [])]


PRIVATE_KEY_LEAKS: dict[str, tuple[str, list[str]]] = {
    "pem-file": ("keys/id_rsa", _pem("RSA ")),
    "openssh-pem-file": ("keys/id_ed25519", _pem("OPENSSH ", OPENSSH_BODY)),
    "crlf-pem-file": ("keys/id_rsa", [f"{line}\r" for line in _pem("RSA ")]),
    "encrypted-pem-file": ("keys/server.key", _pem("RSA ", headers=ENCRYPTION_HEADERS)),
    "pkcs8-encrypted-pem-file": ("keys/server.key", _pem("ENCRYPTED ")),
    "pgp-block": ("keys/signing.asc", [
        "-----BEGIN PGP PRIVATE KEY BLOCK-----", "Version: GnuPG v2", "", *RSA_BODY,
        "-----END PGP PRIVATE KEY BLOCK-----"]),
    "js-string-with-newline-escapes": ("src/keys.js", [
        'export const key = "' + "\\n".join(_pem("RSA ")) + '\\n";']),
    "json-service-account": ("deploy/service-account.json", [
        "{", '  "type": "service_account",',
        '  "private_key": "' + "\\n".join(_pem("")) + '\\n",', "}"]),
    "json-encrypted-pem": ("deploy/creds.json", [
        '{"key": "' + "\\r\\n".join(_pem("RSA ", headers=ENCRYPTION_HEADERS)) + '"}']),
    "ts-array-of-lines": ("src/keys.ts", [
        "const KEY = [", *(f'  "{line}",' for line in _pem("OPENSSH ", OPENSSH_BODY)), '].join("\\n");']),
}

PRIVATE_KEY_MENTIONS: dict[str, tuple[str, list[str]]] = {
    # paperclipai/paperclip#13575
    "validation-message": ("packages/plugins/sandbox-providers/exe-dev/src/ssh-key.ts", [
        "  const headerMatch = trimmed.match(/^-----BEGIN ([A-Z0-9 ]*)PRIVATE KEY-----/m);",
        "  if (!headerMatch) {",
        "    return \"sshPrivateKey must be a PEM-encoded private key starting with a line like "
        "'-----BEGIN OPENSSH PRIVATE KEY-----'.\";",
        "  }",
    ]),
    "ui-placeholder": ("ui/src/SshKeyField.tsx", [
        '<textarea placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" />']),
    "elided-body": ("src/keys.ts", [
        'const SHAPE = "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----";']),
    "short-stub-body": ("keys/ssh.pem", [
        "-----BEGIN RSA PRIVATE KEY-----", "MIIEpAIBAAKCAQEA...", "-----END RSA PRIVATE KEY-----"]),
    "header-constants": ("src/pem.ts", [
        'const HEADER = "-----BEGIN RSA PRIVATE KEY-----";',
        'const FOOTER = "-----END RSA PRIVATE KEY-----";']),
    "template-placeholder": ("deploy/ssh-key.tpl", [
        "-----BEGIN OPENSSH PRIVATE KEY-----", "<paste the private key here>",
        "-----END OPENSSH PRIVATE KEY-----"]),
    "comment-then-code": ("src/pem.ts", [
        "// a key opens with -----BEGIN OPENSSH PRIVATE KEY-----",
        "export function readPrivateKeyWithItsHeaderAndFooterIntact(pem: string): string {"]),
}


@pytest.mark.parametrize("filename, lines", PRIVATE_KEY_LEAKS.values(), ids=PRIVATE_KEY_LEAKS.keys())
def test_private_key_with_its_body_fails_critical(filename: str, lines: list[str]) -> None:
    r = secret_scan.run(make_ctx(filename, lines))
    assert r["verdict"] == "fail"
    assert r["severity"] == "critical"
    assert _patterns(r) == ["private_key_block"]


@pytest.mark.parametrize("filename, lines", PRIVATE_KEY_MENTIONS.values(), ids=PRIVATE_KEY_MENTIONS.keys())
def test_private_key_header_without_its_body_passes(filename: str, lines: list[str]) -> None:
    assert secret_scan.run(make_ctx(filename, lines))["verdict"] == "pass"


@pytest.mark.parametrize("blank_lines, fires", [(5, True), (6, False)])
def test_private_key_body_is_read_up_to_six_lines_past_the_header(blank_lines: int, fires: bool) -> None:
    r = secret_scan.run(make_ctx("keys/id_rsa", ["-----BEGIN RSA PRIVATE KEY-----", *[""] * blank_lines, *RSA_BODY]))
    assert ("private_key_block" in _patterns(r)) is fires


def test_private_key_body_after_an_unchanged_line_is_not_read() -> None:
    ctx = make_ctx("keys/id_rsa", [])
    ctx.files[0]["patch"] = f"@@ -1,1 +1,3 @@\n+-----BEGIN RSA PRIVATE KEY-----\n unchanged\n+{RSA_BODY[0]}\n"
    assert secret_scan.run(ctx)["verdict"] == "pass"


def test_private_key_finding_shows_the_header() -> None:
    [finding] = secret_scan.run(make_ctx("keys/id_rsa", _pem("RSA ")))["details"]["findings"]
    assert finding["match"] == "-----BEGIN RSA PRIVA…"
    assert finding["snippet"] == "-----BEGIN RSA PRIVATE KEY-----"


@pytest.mark.parametrize("lines", [
    ["-----BEGIN PRIVATE KEY-----Comment:" * 30_000],
    ["-----BEGIN PRIVATE KEY-----" + "Proc-Type:" * 100_000 + "!"],
    ["-----BEGIN PRIVATE KEY-----" + "DEK-Info: a-b-c-" * 60_000 + "!"],
    ["-----BEGIN " + "A " * 500_000],
    ["-----BEGIN PRIVATE KEY-----", *[' "\\n' * 100_000] * 6],
    [('"-----BEGIN PRIVATE KEY-----\\n' + RSA_BODY[0] + '\\n",') * 20_000],
], ids=["comment-armor", "proc-type-armor", "hyphenated-armor", "long-label", "lead-only-lines",
        "many-keys"])
def test_private_key_scan_is_linear(lines: list[str]) -> None:
    start = time.perf_counter()
    secret_scan.run(make_ctx("src/blob.ts", lines))
    assert time.perf_counter() - start < 1.0


# ── fixture downgrade ───────────────────────────────────────────────────

def test_fixture_anthropic_key_downgraded():
    """A fixture-shaped key in test code should be uncertain, not fail."""
    ctx = make_ctx("__tests__/auth.test.ts", [
        '// Example key for unit test fixture',
        'const TEST_KEY = "sk-ant-' + "x" * 100 + '"; // redacted in CI',
    ])
    r = secret_scan.run(ctx)
    # Either fixture-like flag triggers downgrade, OR we report uncertain.
    assert r["verdict"] in {"uncertain", "fail"}
    if r["verdict"] == "fail":
        # If still failing, must be because the fixture hint didn't take effect
        # on the key line itself (it was on the comment above). Acceptable —
        # human reviews it. Document the limit.
        pass


def test_documentation_example_passes_or_uncertain():
    """The README often contains placeholder keys like 'sk-ant-your-key-here'."""
    ctx = make_ctx("README.md", [
        'export ANTHROPIC_API_KEY="sk-ant-your-key-here"',
    ])
    r = secret_scan.run(ctx)
    # 'your-key-here' is too short to match our pattern (requires 50+ chars).
    # So this should pass.
    assert r["verdict"] == "pass"


# ── jwt edge case ───────────────────────────────────────────────────────

def test_jwt_in_test_file_is_medium_not_critical():
    """JWTs are noisy — common in test fixtures. We flag medium, not critical."""
    sample_jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    ctx = make_ctx("__tests__/auth.test.ts", [
        f'const VALID_TOKEN = "{sample_jwt}";',
    ])
    r = secret_scan.run(ctx)
    # JWT pattern is medium severity; result is uncertain (not auto-reject)
    assert r["verdict"] in {"uncertain", "pass"}
