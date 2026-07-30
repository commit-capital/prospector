"""The ONE path→risk-tier accessor for triaged upstream PRs.

Tier ranks a PR's blast radius from the upstream file paths it touches — 0
(orchestration/auth core and supply chain) down to 3 (leaf surfaces whose
failures are visible and reversible). It is an ordering/attention signal:
the app's Easy Lane floats tier-3 PRs and the security driver reviews
tier-0 candidates first. It is NOT a gate input — no merge, threat, or
security policy consumes it, and the threat scan stays path-blind (a
"docs-only" diff is still scanned; path shape is attacker-controlled).

The glob map is repository policy in the active profile
(pipeline/profile.py `risk_tiers`); the generic default knows only the
ecosystem-generic supply-chain surface at tier 0. codeowners.py stays the
sole authority for merge routing; overlap with its gated globs on the
supply-chain surface is deliberate — codeowners routes the merge, tier
ranks the risk.
"""
from __future__ import annotations

from pipeline import diffpaths, profile


def classify_path(path: str) -> int:
    """Risk tier (0 = highest risk … 3 = leaf) of one changed upstream path.
    Tier-0/1 globs win over the test-file convention, which wins over the
    tier-3 leaf globs; agent-executed instruction paths pin at the default
    tier even when they look like test/doc files (the content is executed,
    not just read); unmatched paths land on the default tier."""
    rt = profile.active().risk_tiers
    p = diffpaths.normalize_path(path)
    if not p:
        return rt.default_tier
    if any(diffpaths.matches_glob(p, g) for g in rt.tier0_globs):
        return 0
    if any(diffpaths.matches_glob(p, g) for g in rt.tier1_globs):
        return 1
    if any(diffpaths.matches_glob(p, g) for g in rt.instruction_globs):
        return rt.default_tier
    if diffpaths.is_test_path(p) or any(diffpaths.matches_glob(p, g) for g in rt.tier3_globs):
        return 3
    return rt.default_tier


def pr_tier(paths: list[str]) -> int | None:
    """A PR's tier: the most severe (lowest) tier across its changed paths.
    None for an empty list — no known file list means the tier is unknown,
    which is never fast-track-eligible."""
    if not paths:
        return None
    return min(classify_path(p) for p in paths)


def tier_facet(paths: list[str]) -> dict:
    """{"tier": int | None, "pinned_by": [paths]} — the PR's tier plus the
    changed paths sitting at that tier, so a UI can show why."""
    if not paths:
        return {"tier": None, "pinned_by": []}
    by_path = [(p, classify_path(p)) for p in paths]
    tier = min(t for _, t in by_path)
    return {"tier": tier, "pinned_by": [p for p, t in by_path if t == tier]}
