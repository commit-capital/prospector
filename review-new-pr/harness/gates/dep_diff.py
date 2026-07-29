"""dep_diff gate — flag PRs that introduce new dependencies.

Strategy: when a PR modifies `package.json`, parse the dependency section
diff and identify newly-added direct deps. Cross-check each new dep against
an allowlist of pre-vetted packages. New unfamiliar deps -> uncertain
(needs human review) or fail (suspicious).

This gate is intentionally conservative — it does NOT auto-fetch from npm
registry (that's network egress and a separate process). It compares the
diff structurally against a static allowlist that the maintainer team
curates.

In the GitHub Actions deployment, a separate downstream check can enrich
findings with npm registry metadata (download count, maintainer age, etc.)
once we have the orphan-branch verdict ingest pipeline running.
"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
from ._common import PRContext, gate_result


# Pre-vetted package allowlist. Conservative — only packages already in the
# repository's dependency tree or well-known OSS packages from established
# maintainers.
#
# In the deployed harness this lives in a separate config file that the
# maintainer team edits. Bundled inline here for the MVP.
ALLOWLIST: set[str] = set()  # populated below from packages/*/package.json


# Heuristic typosquat-shaped names. Production version of this gate should
# query the npm registry for download counts + maintainer age. Pure-pattern
# checks have a high false-positive rate against legitimate packages like
# `i18next` or `e2b`, so we keep only the very narrow patterns. Pattern
# matching alone is the wrong tool here; the allowlist + npm registry check
# do the real work.
SUSPICIOUS_NAME_PATTERNS = [
    re.compile(r"(.)\1{6,}"),  # 7+ repeated chars (very unusual in real names)
]


_DEP_SECTIONS = {"dependencies", "devDependencies", "peerDependencies", "optionalDependencies"}


def _extract_added_deps_from_patch(patch: str) -> list[tuple[str, str]]:
    """Heuristically extract '"pkg-name": "^version"' lines added by the patch.

    Returns list of (package_name, version_spec) tuples for added lines.
    Doesn't try to parse full package.json — just looks at added lines that
    look like a dep entry.
    """
    dep_line_re = re.compile(r'^\+\s*"([@a-z0-9][a-z0-9@/_.-]*)"\s*:\s*"([^"]+)"\s*,?\s*$', re.I)
    section_re = re.compile(r'"(dependencies|devDependencies|peerDependencies|optionalDependencies)"\s*:\s*\{')

    added: list[tuple[str, str]] = []
    in_dep_section = False
    for line in patch.split("\n"):
        if line.startswith("+++"):
            continue
        # Track which section we're in. The hunk header `@@ ... @@` resets
        # context heuristically. We use a simple state: any added or
        # context line that mentions a dep section opens the state.
        if section_re.search(line):
            in_dep_section = True
            continue
        if line.strip() == "}":  # closing brace likely ends section
            in_dep_section = False
            continue
        # Even without section state, treat strictly-shaped added lines as deps
        m = dep_line_re.match(line)
        if m and (in_dep_section or _looks_like_dep_value(m.group(2))):
            pkg, ver = m.group(1), m.group(2)
            # Skip entries that look like scripts/config (filtered by section state)
            added.append((pkg, ver))
    return added


def _looks_like_dep_value(v: str) -> bool:
    """A version spec like ^1.2.3 / ~1.0 / 1.x / workspace:* / file:..."""
    return bool(re.match(r"^([\^~><=]*\d|\*|workspace:|file:|github:|npm:|git\+|http)", v))


def _is_suspicious_name(name: str) -> bool:
    name_clean = name.lstrip("@").lower()
    return any(p.search(name_clean) for p in SUSPICIOUS_NAME_PATTERNS)


def _populate_allowlist_from_repo() -> None:
    """Walk a local checkout's package.json files to seed the allowlist with
    current deps. Intended for local testing only: the checkout path comes from
    HARNESS_DEP_SEED_REPO. In CI we ship the allowlist as a static file
    generated from the default branch.
    """
    seed = os.environ.get("HARNESS_DEP_SEED_REPO", "")
    if not seed:
        return  # no local checkout configured; allowlist stays empty for tests
    fork_root = Path(seed).expanduser()
    if not fork_root.exists():
        return
    for pkg in fork_root.rglob("package.json"):
        if "node_modules" in pkg.parts:
            continue
        try:
            data = json.loads(pkg.read_text())
        except Exception:
            continue
        for section in _DEP_SECTIONS:
            for name in (data.get(section) or {}):
                ALLOWLIST.add(name)


_populate_allowlist_from_repo()


def run(ctx: PRContext) -> dict:
    """Run dep_diff against a PR context. Returns gate_result dict."""
    package_json_files = [
        f for f in (ctx.files or [])
        if (f.get("filename") or "").endswith("package.json")
    ]
    if not package_json_files:
        return gate_result("pass", evidence="No package.json modified.")

    all_added: list[dict] = []
    for f in package_json_files:
        patch = f.get("patch") or ""
        for pkg, ver in _extract_added_deps_from_patch(patch):
            all_added.append({
                "package": pkg,
                "version": ver,
                "filename": f.get("filename"),
                "in_allowlist": pkg in ALLOWLIST,
                "suspicious_name": _is_suspicious_name(pkg),
            })

    if not all_added:
        return gate_result(
            "pass",
            evidence="package.json modified but no new dependency entries detected.",
        )

    new_unallowed = [d for d in all_added if not d["in_allowlist"]]
    suspicious = [d for d in all_added if d["suspicious_name"]]

    if suspicious:
        first = suspicious[0]
        return gate_result(
            "fail",
            severity="high",
            evidence=(
                f"Adds {len(suspicious)} dep(s) with suspicious-shaped names "
                f"(e.g. '{first['package']}'). Possible typosquat. Requires "
                "manual verification against npm registry."
            ),
            details={"added": all_added},
        )

    if new_unallowed:
        names = ", ".join(d["package"] for d in new_unallowed[:5])
        more = (
            f" (+{len(new_unallowed)-5} more)" if len(new_unallowed) > 5 else ""
        )
        return gate_result(
            "uncertain",
            severity="medium",
            evidence=(
                f"Adds {len(new_unallowed)} dep(s) not on current allowlist: "
                f"{names}{more}. Maintainer should verify each against npm "
                "registry (download count, maintainer age, advisory list) "
                "before merge."
            ),
            details={"added": all_added},
        )

    return gate_result(
        "pass",
        evidence=f"Adds {len(all_added)} dep(s), all on allowlist.",
        details={"added": all_added},
    )
