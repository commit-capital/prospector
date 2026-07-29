"""Tests for the dep_diff gate.

Validates that new direct dependencies are detected and classified.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gates._common import PRContext  # noqa: E402
from gates import dep_diff  # noqa: E402


def make_ctx(patch: str) -> PRContext:
    return PRContext(
        number=9999,
        title="t",
        body="",
        author="a",
        head_sha="x",
        base_ref="master",
        additions=0,
        deletions=0,
        changed_files=1,
        files=[{"filename": "package.json", "status": "modified", "additions": 1, "deletions": 0, "patch": patch}],
    )


# ── happy path ──────────────────────────────────────────────────────────

def test_no_package_json_passes():
    ctx = PRContext(
        number=1, title="t", body="", author="a", head_sha="x", base_ref="master",
        additions=0, deletions=0, changed_files=0, files=[
            {"filename": "src/foo.ts", "status": "modified", "additions": 1, "deletions": 0, "patch": "+const x = 1;\n"},
        ]
    )
    r = dep_diff.run(ctx)
    assert r["verdict"] == "pass"


def test_package_json_modified_but_no_deps_changed():
    # Just a script update, not a dep entry
    patch = (
        "@@ -10,3 +10,4 @@\n"
        '   "scripts": {\n'
        '+    "newscript": "echo hi",\n'
        '     "build": "tsc"\n'
        '   }\n'
    )
    ctx = make_ctx(patch)
    r = dep_diff.run(ctx)
    assert r["verdict"] == "pass"


# ── malicious / suspicious detection ────────────────────────────────────

def test_adds_unknown_dep_flagged_uncertain():
    patch = (
        "@@ -20,5 +20,6 @@\n"
        '   "dependencies": {\n'
        '     "lodash": "^4.17.21",\n'
        '+    "definitely-not-on-allowlist-xyz": "^1.0.0",\n'
        '     "express": "^4.18.0"\n'
        '   }\n'
    )
    ctx = make_ctx(patch)
    r = dep_diff.run(ctx)
    assert r["verdict"] in {"uncertain", "fail"}  # depending on allowlist state
    if "details" in r and r["details"].get("added"):
        names = [d["package"] for d in r["details"]["added"]]
        assert "definitely-not-on-allowlist-xyz" in names


def test_extreme_typosquat_pattern_flagged():
    """7+ repeated chars is unambiguous typosquat shape."""
    patch = (
        "@@ -20,5 +20,6 @@\n"
        '   "dependencies": {\n'
        '+    "aaaaaaaa": "^1.0.0",\n'    # 7+ repeated chars
        '   }\n'
    )
    ctx = make_ctx(patch)
    r = dep_diff.run(ctx)
    assert r["verdict"] == "fail"
    assert r["severity"] == "high"


def test_legitimate_short_digit_package_not_flagged():
    """Real packages like `i18next` and `e2b` shouldn't be flagged as typosquat.

    They should pass through to the allowlist check (uncertain if not allowlisted).
    """
    patch = (
        "@@ -20,5 +20,6 @@\n"
        '   "dependencies": {\n'
        '+    "i18next": "^23.0.0",\n'
        '   }\n'
    )
    ctx = make_ctx(patch)
    r = dep_diff.run(ctx)
    # Should be pass (if allowlist has it) or uncertain — never fail/high.
    assert r["verdict"] in {"pass", "uncertain"}, (
        f"Legitimate package i18next should not auto-reject; got {r}"
    )


def test_existing_allowlist_dep_modification_passes():
    """Modifying an existing allowlist dep (version bump) should NOT trigger.

    Note: our gate only detects newly *added* lines. A version bump shows as
    an added + removed line pair, so the added line for the new version IS
    detected. But the package name should be on the allowlist already.

    This test ensures that an allowlisted dep doesn't trigger uncertain.
    """
    # Force a known-allowlist entry into the allowlist for the test
    dep_diff.ALLOWLIST.add("lodash")
    patch = (
        "@@ -20,3 +20,3 @@\n"
        '-    "lodash": "^4.17.20",\n'
        '+    "lodash": "^4.17.21",\n'
    )
    ctx = make_ctx(patch)
    r = dep_diff.run(ctx)
    assert r["verdict"] == "pass"
